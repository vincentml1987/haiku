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
import json
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
    return conn


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


def register_participant(conn, name: str, kind: str, address: str | None = None) -> str:
    """Registers a new participant (or re-registers, rotating its token —
    callers should register once and hold onto the returned token).
    Returns the plaintext token; only its hash is ever stored."""
    if kind not in ("human", "ai"):
        raise HaikuError(f"invalid participant kind: {kind}")
    existing = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (name,)
    ).fetchone()
    if existing is not None and existing["kind"] != kind:
        raise HaikuError(
            f"{name} is already registered as {existing['kind']}, cannot re-register as {kind}"
        )
    token = secrets.token_urlsafe(32)
    with _transaction(conn):
        conn.execute(
            """INSERT INTO participants (name, kind, address, token_hash) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET address = excluded.address,
                                                token_hash = excluded.token_hash""",
            (name, kind, address, _hash_token(token)),
        )
    return token


def authenticate(conn, name: str, token: str) -> str:
    """Returns the participant's kind on success, raises HaikuError otherwise."""
    row = conn.execute(
        "SELECT kind, token_hash FROM participants WHERE name = ?", (name,)
    ).fetchone()
    if row is None or row["token_hash"] != _hash_token(token):
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


def join_room(conn, room_id: str, participant: str, token: str,
              catch_up: int | None = None) -> None:
    """Explicit join, per spec §2. `catch_up`: for a genuinely first-time
    join, the cursor starts at (latest_seq - catch_up) instead of 0, so the
    first read only surfaces the last N events. A rejoin ignores catch_up
    and keeps the cursor it already has ("everything since it last left")."""
    authenticate(conn, participant, token)
    _get_room(conn, room_id)
    with _transaction(conn):
        had_cursor = conn.execute(
            "SELECT 1 FROM cursors WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        ).fetchone() is not None
        _join(conn, room_id, participant)
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


def room_roster(conn, room_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT participant, status, owes_reply_to_seq
           FROM roster WHERE room_id = ? ORDER BY participant""",
        (room_id,),
    ).fetchall()
    return [dict(r) for r in rows]
