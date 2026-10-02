"""
HAIKU daemon core: room/event operations over the SQLite schema.

Implements docs/haiku-room-spec.md. This module is the only place that
touches obligation/hop-cap/cursor/auth logic directly — the HTTP layer
(server.py) should just translate requests into these calls.

Every mutating call authenticates a (name, token) pair first. `author` is
never trusted bare — a caller cannot post as, or resume as, a participant
it doesn't hold the token for. This is what makes the hop cap and
human-only resume actually mean something (reviewed in by Vero).
"""

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # manual tx control, see _transaction
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    _ensure_admin_secret(conn, db_path)
    return conn


def _admin_secret_path(db_path: str) -> Path:
    return Path(str(db_path) + ".admin_secret")


def _ensure_admin_secret(conn, db_path: str):
    """The admin secret gates human registration and token recovery (see
    register_human, rotate_token). Generated once per database, written
    plaintext to a local file next to the db — never stored in the db
    itself, only its hash is. That file must never be committed; it's
    covered by .gitignore (daemon/*.admin_secret)."""
    secret_path = _admin_secret_path(db_path)
    if secret_path.exists():
        secret = secret_path.read_text().strip()
    else:
        secret = secrets.token_urlsafe(32)
        secret_path.write_text(secret)
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms (e.g. Windows)
    conn.execute(
        """INSERT INTO daemon_config (k, v) VALUES ('admin_secret_hash', ?)
           ON CONFLICT(k) DO UPDATE SET v = excluded.v""",
        (_hash_token(secret),),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class HaikuError(Exception):
    """Raised for rule violations the caller should see as a clear result,
    not a stack trace (e.g. room paused, bad credentials, unknown room)."""


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    """Every public op runs inside one BEGIN IMMEDIATE..COMMIT. Single-writer
    is fine at this scale and closes the races Vero's review flagged: two
    concurrent sends colliding on (room_id, seq), or a crash between the
    event insert and the room hop_count update leaving them inconsistent."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_admin_secret(conn, secret: str) -> bool:
    row = conn.execute(
        "SELECT v FROM daemon_config WHERE k = 'admin_secret_hash'"
    ).fetchone()
    return row is not None and hmac.compare_digest(row["v"], _hash_token(secret))


def _name_taken(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM participants WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone() is not None


def register_ai(conn, name: str, address: str | None = None) -> str:
    """Open self-registration — any caller can claim a brand-new AI name.
    This is deliberately NOT how humans are created (see register_human):
    open registration is the identity back door Vero's review found, so
    it's scoped to the one kind where squatting a fresh name has no
    real stakes attached yet (no room history, no standing obligations)."""
    if _name_taken(conn, name):
        raise HaikuError(f"{name} is already registered — use rotate_token to recover access")
    token = secrets.token_urlsafe(32)
    with _transaction(conn):
        conn.execute(
            "INSERT INTO participants (name, kind, address, token_hash) VALUES (?, 'ai', ?, ?)",
            (name, address, _hash_token(token)),
        )
    return token


def register_human(conn, name: str, admin_secret: str, address: str | None = None) -> str:
    """The only way to create a human participant. Requires the daemon's
    admin secret (see _ensure_admin_secret) — never reachable by an AI
    that only holds its own participant token."""
    if not _verify_admin_secret(conn, admin_secret):
        raise HaikuError("invalid admin secret")
    if _name_taken(conn, name):
        raise HaikuError(f"{name} is already registered — use rotate_token to recover access")
    token = secrets.token_urlsafe(32)
    with _transaction(conn):
        conn.execute(
            "INSERT INTO participants (name, kind, address, token_hash) VALUES (?, 'human', ?, ?)",
            (name, address, _hash_token(token)),
        )
    return token


def rotate_token(conn, name: str, credential: str, credential_is_admin_secret: bool = False) -> str:
    """Re-issues a participant's token. Proves the right to do so either by
    presenting the CURRENT valid token, or — for recovery, e.g. Teddy lost
    his token file — the admin secret. Never open on name alone; that was
    the back door (anyone could call the old register_participant("Teddy",
    ...) and seize the name)."""
    row = conn.execute(
        "SELECT kind, token_hash FROM participants WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {name}")
    if credential_is_admin_secret:
        if not _verify_admin_secret(conn, credential):
            raise HaikuError("invalid admin secret")
    elif not hmac.compare_digest(row["token_hash"], _hash_token(credential)):
        raise HaikuError("invalid credentials")
    token = secrets.token_urlsafe(32)
    with _transaction(conn):
        conn.execute(
            "UPDATE participants SET token_hash = ? WHERE name = ?",
            (_hash_token(token), name),
        )
    return token


def authenticate(conn, name: str, token: str) -> str:
    """Returns the participant's kind on success, raises HaikuError otherwise."""
    row = conn.execute(
        "SELECT kind, token_hash FROM participants WHERE name = ?", (name,)
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
        raise HaikuError("invalid credentials")
    return row["kind"]


def _require_member(conn, room_id: str, participant: str):
    row = conn.execute(
        "SELECT status FROM roster WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    ).fetchone()
    if row is None or row["status"] == "left":
        raise HaikuError(f"{participant} is not a member of this room")


def create_room(conn, name: str, created_by: str, token: str, topic: str | None = None,
                 mode: str = "closed", hop_limit: int = 6) -> str:
    authenticate(conn, created_by, token)
    room_id = str(uuid.uuid4())
    with _transaction(conn):
        conn.execute(
            """INSERT INTO rooms (id, name, topic, created_by, mode, hop_limit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (room_id, name, topic, created_by, mode, hop_limit),
        )
        _join(conn, room_id, created_by)
    return room_id


def _get_room(conn, room_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise HaikuError(f"no such room: {room_id}")
    return row


def _next_seq(conn, room_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM events WHERE room_id = ?",
        (room_id,),
    ).fetchone()
    return row["n"] + 1


def _insert_event(conn, room_id, author, author_kind, type_, addressed_to=None, body=None) -> int:
    seq = _next_seq(conn, room_id)
    conn.execute(
        """INSERT INTO events (room_id, seq, ts, author, author_kind, type, addressed_to, body)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (room_id, seq, _now(), author, author_kind,
         type_, json.dumps(addressed_to) if addressed_to else None, body),
    )
    return seq


def _join(conn, room_id, participant):
    """Internal — must run inside a caller's transaction."""
    row = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (participant,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {participant}")
    _insert_event(conn, room_id, participant, row["kind"], "join")
    conn.execute(
        """INSERT INTO roster (room_id, participant, status, joined_at)
           VALUES (?, ?, 'present', ?)
           ON CONFLICT(room_id, participant) DO UPDATE SET status = 'present'""",
        (room_id, participant, _now()),
    )
    conn.execute(
        """INSERT INTO cursors (room_id, participant, last_delivered_seq)
           VALUES (?, ?, 0)
           ON CONFLICT(room_id, participant) DO NOTHING""",
        (room_id, participant),
    )


def _was_ever_member(conn, room_id: str, participant: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM roster WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    ).fetchone() is not None


def _has_invite(conn, room_id: str, participant: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM invites WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    ).fetchone() is not None


def invite(conn, room_id: str, inviter: str, token: str, invitee: str) -> None:
    """Spec §1/§2: closed = Teddy admits. v1 rule: in a closed room, only
    a present human MEMBER can invite (not just any human who knows the
    room id)."""
    kind = authenticate(conn, inviter, token)
    _get_room(conn, room_id)
    _require_member(conn, room_id, inviter)
    if kind != "human":
        raise HaikuError("only a human member can invite into a closed room")
    with _transaction(conn):
        conn.execute(
            """INSERT INTO invites (room_id, participant, invited_by) VALUES (?, ?, ?)
               ON CONFLICT(room_id, participant) DO UPDATE SET invited_by = excluded.invited_by""",
            (room_id, invitee, inviter),
        )


def join_room(conn, room_id: str, participant: str, token: str,
              catch_up: int | None = None) -> None:
    """Explicit join, per spec §2. A closed room requires the joiner be
    its creator, a past member (rejoining), or holder of a standing
    invite — otherwise this is the back door Vero's review found: any
    locally-registered AI could join and read any closed room's log.
    `catch_up`: for a genuinely first-time join, the cursor starts at
    (latest_seq - catch_up) instead of 0, so the first read only surfaces
    the last N events. A rejoin ignores catch_up and keeps the cursor it
    already has ("everything since it last left")."""
    authenticate(conn, participant, token)
    room = _get_room(conn, room_id)
    if room["mode"] == "closed" and participant != room["created_by"]:
        if not (_was_ever_member(conn, room_id, participant) or _has_invite(conn, room_id, participant)):
            raise HaikuError("room is closed; ask a human member to invite you")
    with _transaction(conn):
        had_cursor = conn.execute(
            "SELECT 1 FROM cursors WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        ).fetchone() is not None
        _join(conn, room_id, participant)
        conn.execute(
            "DELETE FROM invites WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        )
        if not had_cursor and catch_up is not None:
            latest = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM events WHERE room_id = ?",
                (room_id,),
            ).fetchone()["n"]
            start = max(0, latest - catch_up)
            conn.execute(
                "UPDATE cursors SET last_delivered_seq = ? WHERE room_id = ? AND participant = ?",
                (start, room_id, participant),
            )


def leave_room(conn, room_id: str, participant: str, token: str) -> None:
    kind = authenticate(conn, participant, token)
    _get_room(conn, room_id)
    with _transaction(conn):
        _insert_event(conn, room_id, participant, kind, "leave")
        conn.execute(
            """UPDATE roster SET status = 'left', owes_reply_to_seq = NULL
               WHERE room_id = ? AND participant = ?""",
            (room_id, participant),
        )


def mark_away(conn, room_id: str, participant: str):
    """A session that stops running becomes 'away', not 'left' (spec §2).
    No event is logged — presence state only, not a room action. Intended
    as a daemon-internal signal (e.g. a liveness check), not a public
    HTTP endpoint for arbitrary callers — server.py should not expose this
    without its own admin auth."""
    with _transaction(conn):
        conn.execute(
            """UPDATE roster SET status = 'away'
               WHERE room_id = ? AND participant = ? AND status = 'present'""",
            (room_id, participant),
        )


def _present_ai_participants(conn, room_id: str, exclude: str | None = None) -> list[str]:
    rows = conn.execute(
        """SELECT r.participant FROM roster r
           JOIN participants p ON p.name = r.participant
           WHERE r.room_id = ? AND r.status != 'left' AND p.kind = 'ai'
             AND r.participant != COALESCE(?, '')""",
        (room_id, exclude),
    ).fetchall()
    return [r["participant"] for r in rows]


def _validate_addressees(conn, room_id: str, names: list[str]):
    if names == ["all"]:
        return
    for name in names:
        row = conn.execute(
            "SELECT status FROM roster WHERE room_id = ? AND participant = ?",
            (room_id, name),
        ).fetchone()
        if row is None or row["status"] == "left":
            raise HaikuError(f"cannot address {name}: not a current member of this room")


def _set_obligation(conn, room_id: str, participant: str, seq: int | None):
    conn.execute(
        """UPDATE roster SET owes_reply_to_seq = ?
           WHERE room_id = ? AND participant = ?""",
        (seq, room_id, participant),
    )


def send_message(conn, room_id: str, author: str, token: str, body: str,
                  addressed_to: list[str] | None = None) -> dict:
    """addressed_to: None (unaddressed), ['all'], or a list of participant
    names. Implements spec §3 turn-taking and §3.3 hop cap."""
    author_kind = authenticate(conn, author, token)
    room = _get_room(conn, room_id)
    if room["state"] == "archived":
        raise HaikuError("room is archived")
    _require_member(conn, room_id, author)
    if room["state"] == "paused" and author_kind == "ai":
        raise HaikuError("room paused, waiting on Teddy")
    if addressed_to:
        _validate_addressees(conn, room_id, addressed_to)

    with _transaction(conn):
        seq = _insert_event(conn, room_id, author, author_kind, "message",
                             addressed_to=addressed_to, body=body)

        if author_kind == "human":
            # Newest human message wins (spec §3.1/§4 simplification): clear
            # every present AI's outstanding obligation before reassigning,
            # so "addressed someone else" genuinely discharges the others.
            new_hop_count = 0
            for name in _present_ai_participants(conn, room_id):
                _set_obligation(conn, room_id, name, None)
            if addressed_to and addressed_to != ["all"]:
                for name in addressed_to:
                    _set_obligation(conn, room_id, name, seq)
            else:
                for name in _present_ai_participants(conn, room_id, exclude=author):
                    _set_obligation(conn, room_id, name, seq)
            new_state = room["state"]
        else:
            _set_obligation(conn, room_id, author, None)  # own message discharges own obligation
            if addressed_to and addressed_to != ["all"]:
                for name in addressed_to:
                    if name != author:
                        _set_obligation(conn, room_id, name, seq)
            new_hop_count = room["hop_count"] + 1
            new_state = room["state"]
            if new_hop_count >= room["hop_limit"]:
                new_state = "paused"
                _insert_event(conn, room_id, author, author_kind, "pause",
                              body=f"hop cap ({room['hop_limit']}) reached")

        conn.execute(
            "UPDATE rooms SET hop_count = ?, state = ? WHERE id = ?",
            (new_hop_count, new_state, room_id),
        )

    return {"seq": seq, "room_state": new_state}


def send_pass(conn, room_id: str, author: str, token: str) -> int:
    author_kind = authenticate(conn, author, token)
    room = _get_room(conn, room_id)
    if room["state"] == "archived":
        raise HaikuError("room is archived")
    _require_member(conn, room_id, author)
    if room["state"] == "paused" and author_kind == "ai":
        raise HaikuError("room paused, waiting on Teddy")
    with _transaction(conn):
        seq = _insert_event(conn, room_id, author, author_kind, "pass")
        _set_obligation(conn, room_id, author, None)
    return seq


def resume_room(conn, room_id: str, resumed_by: str, token: str,
                 granted_hops: int | None = None) -> None:
    """Spec §3.3: resume is Teddy-only ('continue' / 'continue N')."""
    kind = authenticate(conn, resumed_by, token)
    if kind != "human":
        raise HaikuError("only a human can resume a paused room")
    room = _get_room(conn, room_id)
    if room["state"] != "paused":
        raise HaikuError(f"room is {room['state']}, not paused")
    new_limit = room["hop_count"] + (granted_hops or room["hop_limit"])
    with _transaction(conn):
        conn.execute(
            "UPDATE rooms SET state = 'active', hop_limit = ? WHERE id = ?",
            (new_limit, room_id),
        )
        _insert_event(conn, room_id, resumed_by, "human", "resume",
                      body=f"resumed, hop_limit now {new_limit}")


def read_events(conn, room_id: str, participant: str, token: str,
                 since: int | None = None, limit: int | None = None,
                 advance: bool = True, exclude_self: bool = False) -> list[dict]:
    """Returns events after `since` (explicit pull) or after the stored
    cursor (default catch-up). With advance=True (default), the cursor is
    moved forward to the last seq actually returned here — never past
    what's merely queued (spec §2). The hook should call with
    advance=False, emit the events into the session, and only then call
    ack() — so a dropped injection doesn't silently lose events."""
    authenticate(conn, participant, token)
    _get_room(conn, room_id)
    _require_member(conn, room_id, participant)

    if since is None:
        cur = conn.execute(
            "SELECT last_delivered_seq FROM cursors WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        ).fetchone()
        since = cur["last_delivered_seq"] if cur else 0

    query = """SELECT seq, ts, author, author_kind, type, addressed_to, body
               FROM events WHERE room_id = ? AND seq > ?"""
    params = [room_id, since]
    if exclude_self:
        query += " AND author != ?"
        params.append(participant)
    query += " ORDER BY seq ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    events = [
        {**dict(r), "addressed_to": json.loads(r["addressed_to"]) if r["addressed_to"] else None}
        for r in rows
    ]

    if advance and rows:
        _ack(conn, room_id, participant, rows[-1]["seq"])

    return events


def _ack(conn, room_id: str, participant: str, through_seq: int):
    with _transaction(conn):
        conn.execute(
            """INSERT INTO cursors (room_id, participant, last_delivered_seq, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(room_id, participant) DO UPDATE SET
                   last_delivered_seq = MAX(last_delivered_seq, excluded.last_delivered_seq),
                   updated_at = excluded.updated_at""",
            (room_id, participant, through_seq, _now()),
        )


def ack(conn, room_id: str, participant: str, token: str, through_seq: int):
    """For the hook: call after events fetched via read_events(advance=False)
    have actually been injected into the session, confirming delivery."""
    authenticate(conn, participant, token)
    _ack(conn, room_id, participant, through_seq)


def set_topic(conn, room_id: str, author: str, token: str, topic: str) -> int:
    author_kind = authenticate(conn, author, token)
    room = _get_room(conn, room_id)
    if room["state"] == "archived":
        raise HaikuError("room is archived")
    _require_member(conn, room_id, author)
    with _transaction(conn):
        seq = _insert_event(conn, room_id, author, author_kind, "topic_change", body=topic)
        conn.execute("UPDATE rooms SET topic = ? WHERE id = ?", (topic, room_id))
    return seq


def get_room(conn, room_id: str) -> dict:
    return dict(_get_room(conn, room_id))


def list_rooms(conn, state: str | None = None) -> list[dict]:
    if state is not None:
        rows = conn.execute("SELECT * FROM rooms WHERE state = ? ORDER BY created_at", (state,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM rooms ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def room_roster(conn, room_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT participant, status, owes_reply_to_seq
           FROM roster WHERE room_id = ? ORDER BY participant""",
        (room_id,),
    ).fetchall()
    return [dict(r) for r in rows]
