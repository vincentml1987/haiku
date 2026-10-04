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
import shutil
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Schema versioning, tracked in SQLite's own PRAGMA user_version (an
# integer baked into the db file, no extra table needed). schema.sql's
# CREATE TABLE IF NOT EXISTS only ever applies to a brand-new db — an
# existing db's tables keep whatever constraints they were created with
# forever, so a change like a CHECK constraint (e.g. adding 'archive' to
# events.type, which broke live on 2026-10-02 before this existed) needs
# an explicit, versioned migration to reach every db that predates it.
#
# CURRENT_SCHEMA_VERSION is "the schema in schema.sql right now". Each
# key in MIGRATIONS is a target version; its function brings a db at
# (that version - 1) up to that version, idempotently, inside its own
# transaction. Version 1 is the implicit baseline: any pre-existing db
# with no user_version set (every db from before this system existed)
# is treated as v1. A db whose user_version is HIGHER than this code
# knows is refused outright rather than run against blindly.
CURRENT_SCHEMA_VERSION = 4


def _migrate_v1_to_v2(conn):
    """Adds 'archive' to events.type's CHECK constraint. SQLite can't ALTER
    a CHECK constraint, so the table is recreated and every row copied."""
    conn.executescript("""
        BEGIN;
        CREATE TABLE events_new (
            room_id     TEXT NOT NULL REFERENCES rooms(id),
            seq         INTEGER NOT NULL,
            ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            author      TEXT NOT NULL REFERENCES participants(name),
            author_kind TEXT NOT NULL CHECK (author_kind IN ('human', 'ai')),
            type        TEXT NOT NULL CHECK (type IN
                            ('message', 'join', 'leave', 'topic_change',
                             'pass', 'pause', 'resume', 'archive')),
            addressed_to TEXT,
            body        TEXT,
            PRIMARY KEY (room_id, seq)
        );
        INSERT INTO events_new SELECT * FROM events;
        DROP TABLE events;
        ALTER TABLE events_new RENAME TO events;
        CREATE INDEX idx_events_room_seq ON events(room_id, seq);
        COMMIT;
    """)


def _migrate_v2_to_v3(conn):
    """Adds participants.wake_allowed (auto-wake kill switch, default allowed)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(participants)")]
    if "wake_allowed" not in cols:
        conn.execute(
            "ALTER TABLE participants ADD COLUMN wake_allowed INTEGER NOT NULL DEFAULT 1 "
            "CHECK (wake_allowed IN (0, 1))"
        )
        conn.commit()


def _migrate_v3_to_v4(conn):
    """Adds room_prefs (per-room mute + per-room wake_allowed). schema.sql
    already creates it IF NOT EXISTS on every connect, so this only has to
    exist to keep the version number honest; it is idempotent either way."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS room_prefs (
            room_id      TEXT NOT NULL REFERENCES rooms(id),
            participant  TEXT NOT NULL REFERENCES participants(name),
            muted        INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
            wake_allowed INTEGER NOT NULL DEFAULT 1 CHECK (wake_allowed IN (0, 1)),
            PRIMARY KEY (room_id, participant)
        )
    """)


MIGRATIONS = {2: _migrate_v1_to_v2, 3: _migrate_v2_to_v3, 4: _migrate_v3_to_v4}


def _migrate(conn, db_path, existed_before: bool):
    if not existed_before:
        # schema.sql just created this db fresh, already at the latest shape.
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        return

    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        current = 1  # pre-existing db from before versioning existed

    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than this code understands "
            f"(max {CURRENT_SCHEMA_VERSION}) — refusing to start against it"
        )

    if current < CURRENT_SCHEMA_VERSION:
        if db_path != ":memory:" and Path(db_path).exists():
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            backup_path = f"{db_path}.backup-before-migration-v{current}-to-v{CURRENT_SCHEMA_VERSION}"
            shutil.copy2(db_path, backup_path)
        for v in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            MIGRATIONS[v](conn)
            conn.execute(f"PRAGMA user_version = {v}")


LOBBY_NAME = "lobby"


def _lobby_row(conn):
    return conn.execute(
        "SELECT * FROM rooms WHERE name = ? COLLATE NOCASE", (LOBBY_NAME,)
    ).fetchone()


def ensure_lobby(conn) -> str | None:
    """docs/haiku-room-spec.md "The lobby": the daemon owns a room named
    `lobby`; every human is a member from the start; it can't be archived;
    the hop cap applies like any room. AIs are NEVER auto-joined — joining
    stays the same explicit call as any room.

    Idempotent, and safe to call on every connect() and after every human
    registration, so it also covers a db that predates the lobby (it needs
    no schema change, just this call). rooms.created_by must reference a
    real participant, so on a brand-new db the lobby can't exist until the
    first human does; register_human calls this to create it then. A human
    who later LEFT the lobby has a roster row (status 'left') and is not
    pulled back in — only humans with no roster row at all are joined.
    Returns the lobby's room id, or None if it can't exist yet."""
    with _transaction(conn):
        room = _lobby_row(conn)
        if room is None:
            first = conn.execute(
                "SELECT name FROM participants WHERE kind = 'human' ORDER BY created_at, name LIMIT 1"
            ).fetchone()
            if first is None:
                return None
            room_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO rooms (id, name, topic, created_by, mode)
                   VALUES (?, ?, ?, ?, 'open')""",
                (room_id, LOBBY_NAME,
                 "Announcements, who's online, and finding each other", first["name"]),
            )
        else:
            room_id = room["id"]
        for h in conn.execute("SELECT name FROM participants WHERE kind = 'human'").fetchall():
            if not _was_ever_member(conn, room_id, h["name"]):
                _join(conn, room_id, h["name"])
    return room_id


def lobby_info(conn) -> dict | None:
    """What registration tells a new participant: the lobby exists and how
    to find it. Never joins anyone."""
    room = _lobby_row(conn)
    if room is None:
        return None
    return {"room_id": room["id"], "name": room["name"], "joined": False}


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # manual tx control, see _transaction
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    existed_before = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone() is not None
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn, db_path, existed_before)
    _ensure_admin_secret(conn, db_path)
    ensure_lobby(conn)
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


class Forbidden(HaikuError):
    """A HaikuError the HTTP layer reports as 403 rather than 400: the
    caller is who they say they are, but isn't allowed to see/do this
    (e.g. reading a room they are not a member of)."""


def _is_active_member(conn, room_id: str, participant: str) -> bool:
    row = conn.execute(
        "SELECT status FROM roster WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    ).fetchone()
    return row is not None and row["status"] != "left"


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


def _validate_display_name(name: str, max_len: int = 64):  # keep equal to NAME_MAX_LEN
    """A name (participant or room) is rendered unfenced in the hook's
    delivery block (spec §4 / hook-format.md) — author names, room names,
    and addressed_to entries all sit outside the "| " body fence. The
    plugin's format.ts sanitizes defensively too, but rejecting a hostile
    name here means bad data never enters the log at all. Vero's review
    found this gap: open AI self-registration meant any name, including
    one containing newlines or a fake event-header line, could land in
    every future delivery to every room that participant joins."""
    if not name or len(name) > max_len:
        raise HaikuError(f"name must be 1-{max_len} characters")
    if any(ord(c) < 0x20 for c in name) or '<' in name:
        raise HaikuError("name may not contain control characters, newlines, or '<'")


def register_ai(conn, name: str, address: str | None = None) -> str:
    """Open self-registration — any caller can claim a brand-new AI name.
    This is deliberately NOT how humans are created (see register_human):
    open registration is the identity back door Vero's review found, so
    it's scoped to the one kind where squatting a fresh name has no
    real stakes attached yet (no room history, no standing obligations)."""
    _validate_display_name(name)
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
    _validate_display_name(name)
    if _name_taken(conn, name):
        raise HaikuError(f"{name} is already registered — use rotate_token to recover access")
    token = secrets.token_urlsafe(32)
    with _transaction(conn):
        conn.execute(
            "INSERT INTO participants (name, kind, address, token_hash) VALUES (?, 'human', ?, ?)",
            (name, address, _hash_token(token)),
        )
    ensure_lobby(conn)
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
    if not _is_active_member(conn, room_id, participant):
        raise Forbidden(f"{participant} is not a member of this room")


def create_room(conn, name: str, created_by: str, token: str, topic: str | None = None,
                 mode: str = "closed", hop_limit: int = 6) -> str:
    authenticate(conn, created_by, token)
    _validate_display_name(name)
    if name.lower() == LOBBY_NAME:
        raise HaikuError(f"'{LOBBY_NAME}' is reserved for the daemon's own lobby room")
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
    """Spec §2 (Teddy, 2026-10-02): in an OPEN room any active member, human
    or AI, may invite any registered participant. In a CLOSED room only a
    present human MEMBER may invite (not just any human who knows the room
    id), so a human stays the gate to private logs. Either way an invite
    is an offer only; the invitee must still join explicitly."""
    kind = authenticate(conn, inviter, token)
    room = _get_room(conn, room_id)
    _require_member(conn, room_id, inviter)
    if room["mode"] == "closed" and kind != "human":
        raise HaikuError("only a human member can invite into a closed room")
    if conn.execute("SELECT 1 FROM participants WHERE name = ?", (invitee,)).fetchone() is None:
        raise HaikuError(f"{invitee} is not a registered participant yet — they must register with the daemon first")
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
    kind = authenticate(conn, participant, token)
    room = _get_room(conn, room_id)
    # A human is HAIKU's admin (Teddy, 2026-10-04): may join any room,
    # closed or not, uninvited. Checked against the AUTHENTICATED kind,
    # never anything the caller asserts. It is an ordinary join, so it
    # lands as a visible join event like any other: no silent lurking.
    if room["mode"] == "closed" and participant != room["created_by"] and kind != "human":
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


# Teddy's call (2026-10-04, haiku-update seq 8): a HUMAN message addressed
# to a muted AI by name still reaches it, so a human can always reach any
# of us; HAIKU exists for exactly that. Unaddressed,
# ["all"], and AI-addressed traffic never break through a mute.
MUTE_HUMAN_ADDRESSED_BREAKS_THROUGH = True


def _is_muted(conn, room_id: str, participant: str) -> bool:
    row = conn.execute(
        "SELECT muted FROM room_prefs WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    ).fetchone()
    return bool(row and row["muted"])


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
                    if MUTE_HUMAN_ADDRESSED_BREAKS_THROUGH or not _is_muted(conn, room_id, name):
                        _set_obligation(conn, room_id, name, seq)
            else:
                for name in _present_ai_participants(conn, room_id, exclude=author):
                    if not _is_muted(conn, room_id, name):
                        _set_obligation(conn, room_id, name, seq)
            new_state = room["state"]
        else:
            _set_obligation(conn, room_id, author, None)  # own message discharges own obligation
            if addressed_to and addressed_to != ["all"]:
                for name in addressed_to:
                    if name != author and not _is_muted(conn, room_id, name):
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


def pause_room(conn, room_id: str, paused_by: str, token: str, reason: str | None = None) -> None:
    """ui-spec.md §5/§7.3: Teddy can always stop a room, not just the hop
    cap. Human-only, same shape as resume_room's own guard."""
    kind = authenticate(conn, paused_by, token)
    if kind != "human":
        raise HaikuError("only a human can pause a room")
    room = _get_room(conn, room_id)
    if room["state"] == "archived":
        raise HaikuError("room is archived")
    if room["state"] == "paused":
        raise HaikuError("room is already paused")
    with _transaction(conn):
        conn.execute("UPDATE rooms SET state = 'paused' WHERE id = ?", (room_id,))
        _insert_event(conn, room_id, paused_by, "human", "pause", body=reason)


# Teddy's call (2026-10-04, haiku-update seq 8): the archive stamp is in
# LOCAL time (the daemon machine's clock), unlike event `ts`, which stays UTC.
ARCHIVE_STAMP_UTC = False
NAME_MAX_LEN = 64


def _archived_name(conn, name: str, when: datetime) -> str:
    """Teddy, 2026-10-04: archiving appends -AYYYYMMDDHHMMSS so the list
    shows when. The room id never changes (cursors, events, invites all key
    on it), only the display name. rooms.name is UNIQUE and capped at
    NAME_MAX_LEN, so the base is trimmed to fit, and a collision (two
    same-named rooms archived in the same second) gets -2, -3, ..."""
    stamp = "-A" + when.strftime("%Y%m%d%H%M%S")
    candidate = name[: NAME_MAX_LEN - len(stamp)] + stamp
    n = 2
    while conn.execute("SELECT 1 FROM rooms WHERE name = ?", (candidate,)).fetchone():
        suffix = f"{stamp}-{n}"
        candidate = name[: NAME_MAX_LEN - len(suffix)] + suffix
        n += 1
    return candidate


def archive_room(conn, room_id: str, archived_by: str, token: str) -> str:
    """ui-spec.md §5/§7.3: ends a room; stays readable, no further sends.
    Human-only. Renames the room (see _archived_name) and records the old
    name in the archive event's body. Returns the new name."""
    kind = authenticate(conn, archived_by, token)
    if kind != "human":
        raise HaikuError("only a human can archive a room")
    room = _get_room(conn, room_id)
    if room["name"].lower() == LOBBY_NAME:
        raise HaikuError("the lobby cannot be archived")
    if room["state"] == "archived":
        raise HaikuError("room is already archived")
    when = datetime.now(timezone.utc) if ARCHIVE_STAMP_UTC else datetime.now()
    with _transaction(conn):
        new_name = _archived_name(conn, room["name"], when)
        conn.execute("UPDATE rooms SET state = 'archived', name = ? WHERE id = ?", (new_name, room_id))
        _insert_event(conn, room_id, archived_by, kind, "archive",
                      body=f"archived; renamed from {room['name']!r} to {new_name!r}")
    return new_name


def read_events(conn, room_id: str, participant: str, token: str,
                 since: int | None = None, limit: int | None = None,
                 advance: bool = True, exclude_self: bool = False) -> dict:
    """Returns {"events": [...], "max_seq": N}. `events` is after `since`
    (explicit pull) or the stored cursor (default catch-up), filtered by
    `exclude_self` if set. `max_seq` is the highest seq SCANNED in this
    call — before exclude_self filtering — so a caller can advance its
    cursor past a self-authored tail even when the visible `events` list
    is empty (Vero's review: otherwise an all-self tail never advances
    the cursor and the hook re-reads it forever). With advance=True
    (default), the cursor moves to `max_seq` — never past what's merely
    queued (spec §2). The hook should call with advance=False, emit the
    events into the session, and only then call ack() — so a dropped
    injection doesn't silently lose events."""
    authenticate(conn, participant, token)
    _get_room(conn, room_id)
    _require_member(conn, room_id, participant)

    if since is None:
        cur = conn.execute(
            "SELECT last_delivered_seq FROM cursors WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        ).fetchone()
        since = cur["last_delivered_seq"] if cur else 0

    query = "SELECT seq, ts, author, author_kind, type, addressed_to, body FROM events WHERE room_id = ? AND seq > ? ORDER BY seq ASC"
    params = [room_id, since]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    candidates = conn.execute(query, params).fetchall()
    max_seq = candidates[-1]["seq"] if candidates else since
    visible = [r for r in candidates if not (exclude_self and r["author"] == participant)]
    events = [
        {**dict(r), "addressed_to": json.loads(r["addressed_to"]) if r["addressed_to"] else None}
        for r in visible
    ]

    if advance and max_seq > since:
        _ack(conn, room_id, participant, max_seq)

    return {"events": events, "max_seq": max_seq}


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


# What a caller who is not (yet) in a room may see of it: just enough to
# decide whether to join. No creator, no hop state, no roster, no events.
_PUBLIC_ROOM_FIELDS = ("id", "name", "topic", "mode", "state")


def _public_room(room) -> dict:
    return {k: room[k] for k in _PUBLIC_ROOM_FIELDS}


def get_room(conn, room_id: str, caller: str | None = None) -> dict:
    """Room metadata, scoped by caller (docs/haiku-room-spec.md: rosters
    are per room, visible to that room's members only). `caller` must
    already be authenticated. A human admin sees everything. An AI that
    is an active member sees the full room. A non-member AI sees only the
    public fields, and only if the room is open or it holds an invite;
    otherwise Forbidden. The roster is attached by the HTTP layer only
    when `include_roster(...)` says so."""
    room = _get_room(conn, room_id)
    if caller is None:
        return dict(room)
    kind = conn.execute("SELECT kind FROM participants WHERE name = ?", (caller,)).fetchone()["kind"]
    if kind == "human" or _is_active_member(conn, room_id, caller):
        return dict(room)
    if room["mode"] == "open" or _has_invite(conn, room_id, caller):
        return _public_room(room)
    raise Forbidden("not a member of this room")


def can_see_roster(conn, room_id: str, caller: str) -> bool:
    kind = conn.execute("SELECT kind FROM participants WHERE name = ?", (caller,)).fetchone()["kind"]
    return kind == "human" or _is_active_member(conn, room_id, caller)

def list_my_rooms(conn, participant: str, token: str) -> list[dict]:
    """ui-spec.md §7.1: for the authenticated participant, every room they
    belong to (not left) with unread, needs_me, state, hop counts and
    last_event_ts — what the room-list UI needs in one call."""
    authenticate(conn, participant, token)
    rows = conn.execute(
        """SELECT rm.id, rm.name, rm.topic, rm.state, rm.hop_count, rm.hop_limit,
                  r.owes_reply_to_seq,
                  COALESCE(c.last_delivered_seq, 0) AS cursor_seq,
                  (SELECT COALESCE(MAX(seq), 0) FROM events WHERE room_id = rm.id) AS max_seq,
                  (SELECT MAX(ts) FROM events WHERE room_id = rm.id) AS last_event_ts,
                  COALESCE(rp.muted, 0) AS muted,
                  COALESCE(rp.wake_allowed, 1) AS room_wake_allowed
           FROM roster r
           JOIN rooms rm ON rm.id = r.room_id
           LEFT JOIN cursors c ON c.room_id = r.room_id AND c.participant = r.participant
           LEFT JOIN room_prefs rp ON rp.room_id = r.room_id AND rp.participant = r.participant
           WHERE r.participant = ? AND r.status != 'left'
           ORDER BY last_event_ts DESC""",
        (participant,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["unread"] = max(0, d["max_seq"] - d["cursor_seq"])
        # Aliases the UI reads (ui-spec.md's own naming): keep both rather
        # than rename and risk the other callers/tests of max_seq/cursor_seq.
        d["last_seq"] = d["max_seq"]
        d["my_cursor"] = d["cursor_seq"]
        d["muted"] = bool(d["muted"])
        d["room_wake_allowed"] = bool(d["room_wake_allowed"])
        # A muted room only "needs me" when someone actually got through
        # (a human addressed me by name); a pause alone doesn't count there.
        d["needs_me"] = d["owes_reply_to_seq"] is not None or (d["state"] == "paused" and not d["muted"])
        result.append(d)
    return result


def set_wake_allowed(conn, caller: str, token: str, target: str, allowed: bool) -> None:
    """Auto-wake kill switch (spec 3a level 3). Human callers only; it can
    only withhold waking, never enable it past the plugin's own levels."""
    if authenticate(conn, caller, token) != "human":
        raise Forbidden("only a human may change wake_allowed")
    cur = conn.execute(
        "UPDATE participants SET wake_allowed = ? WHERE name = ? COLLATE NOCASE",
        (1 if allowed else 0, target),
    )
    if cur.rowcount == 0:
        raise HaikuError(f"no such participant: {target}")
    conn.commit()


def set_room_muted(conn, room_id: str, participant: str, token: str, muted: bool) -> None:
    """An AI (or anyone) mutes a room for ITSELF only: no hook delivery, no
    wake, no obligations from unaddressed traffic. Must be a current
    member; still able to read on demand. Muting also clears any obligation
    it currently owes there, since it has chosen not to be on the hook."""
    authenticate(conn, participant, token)
    _get_room(conn, room_id)
    _require_member(conn, room_id, participant)
    with _transaction(conn):
        conn.execute(
            """INSERT INTO room_prefs (room_id, participant, muted) VALUES (?, ?, ?)
               ON CONFLICT(room_id, participant) DO UPDATE SET muted = excluded.muted""",
            (room_id, participant, 1 if muted else 0),
        )
        if muted:
            _set_obligation(conn, room_id, participant, None)


def set_room_wake_allowed(conn, caller: str, token: str, room_id: str, target: str, allowed: bool) -> None:
    """Teddy, 2026-10-04: per-room, per-AI auto-wake switch. Human callers
    only, restrict-only (ANDed with participants.wake_allowed and the
    plugin's own levels; it can withhold a wake, never cause one)."""
    if authenticate(conn, caller, token) != "human":
        raise Forbidden("only a human may change a room's wake_allowed")
    _get_room(conn, room_id)
    row = conn.execute("SELECT name FROM participants WHERE name = ? COLLATE NOCASE", (target,)).fetchone()
    if row is None:
        raise HaikuError(f"no such participant: {target}")
    with _transaction(conn):
        conn.execute(
            """INSERT INTO room_prefs (room_id, participant, wake_allowed) VALUES (?, ?, ?)
               ON CONFLICT(room_id, participant) DO UPDATE SET wake_allowed = excluded.wake_allowed""",
            (room_id, row["name"], 1 if allowed else 0),
        )


def get_wake_allowed(conn, participant: str) -> bool:
    row = conn.execute(
        "SELECT wake_allowed FROM participants WHERE name = ?", (participant,)
    ).fetchone()
    return bool(row["wake_allowed"]) if row else True


def list_participants(conn, caller: str) -> list[dict]:
    """ui-spec.md "Decisions": scoped by caller. A human (the admin, Teddy)
    sees every registered name and kind. An AI sees only participants who
    currently share at least one room with it (both not 'left'), and never
    which rooms: names + kinds only, never the caller itself. An AI that
    shares no room with anyone gets an empty list. `caller` must already
    be authenticated by whoever calls this."""
    row = conn.execute("SELECT kind FROM participants WHERE name = ?", (caller,)).fetchone()
    if row is None:
        raise HaikuError("invalid credentials")
    if row["kind"] == "human":
        rows = conn.execute("SELECT name, kind, wake_allowed FROM participants ORDER BY name").fetchall()
        return [{"name": r["name"], "kind": r["kind"], "wake_allowed": bool(r["wake_allowed"])} for r in rows]
    else:
        rows = conn.execute(
            """SELECT DISTINCT p.name, p.kind FROM participants p
               JOIN roster other ON other.participant = p.name AND other.status != 'left'
               JOIN roster mine ON mine.room_id = other.room_id
                                AND mine.participant = ? AND mine.status != 'left'
               WHERE p.name != ?
               ORDER BY p.name""",
            (caller, caller),
        ).fetchall()
    return [dict(r) for r in rows]


def list_pending_invites(conn, participant: str) -> list[dict]:
    """Standing invites for `participant`; consumed on join, so this is
    exactly "invited, not yet joined". The room name is what the invite
    itself offers; no room contents or roster are exposed."""
    rows = conn.execute(
        """SELECT i.room_id, rm.name AS room_name, i.invited_by
           FROM invites i JOIN rooms rm ON rm.id = i.room_id
           WHERE i.participant = ? AND rm.state != 'archived'
           ORDER BY i.created_at""",
        (participant,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_rooms(conn, state: str | None = None, caller: str | None = None) -> list[dict]:
    """`caller` (already authenticated) scopes the result. A human sees
    every room in full. An AI sees full rows for rooms it is an active
    member of, public fields (name/topic/mode/state) for open rooms and
    rooms it holds an invite to, and nothing about any other closed room."""
    if state is not None:
        rows = conn.execute("SELECT * FROM rooms WHERE state = ? ORDER BY created_at", (state,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM rooms ORDER BY created_at").fetchall()
    if caller is None:
        return [dict(r) for r in rows]
    kind = conn.execute("SELECT kind FROM participants WHERE name = ?", (caller,)).fetchone()["kind"]
    if kind == "human":
        return [dict(r) for r in rows]
    out = []
    for r in rows:
        if _is_active_member(conn, r["id"], caller):
            out.append(dict(r))
        elif r["mode"] == "open" or _has_invite(conn, r["id"], caller):
            out.append(_public_room(r))
    return out


def room_roster(conn, room_id: str) -> list[dict]:
    """Per ui-spec.md §7.2: kind (for the UI's human/AI badge) and
    last_active_ts (last authored event or last delivery — the daemon
    cannot know a session is mid-turn, so it offers this instead of a
    "busy" guess, per the spec's own call). Obligation rows carry who
    the owed reply is to and when that event happened, not just the seq."""
    rows = conn.execute(
        """SELECT r.participant, p.kind, r.status, r.owes_reply_to_seq,
                  (SELECT MAX(ts) FROM events WHERE room_id = r.room_id AND author = r.participant) AS last_authored_ts,
                  c.updated_at AS cursor_ts,
                  oe.author AS owes_from_author, oe.ts AS owes_from_ts,
                  COALESCE(rp.muted, 0) AS muted,
                  COALESCE(rp.wake_allowed, 1) AS room_wake_allowed
           FROM roster r
           JOIN participants p ON p.name = r.participant
           LEFT JOIN cursors c ON c.room_id = r.room_id AND c.participant = r.participant
           LEFT JOIN events oe ON oe.room_id = r.room_id AND oe.seq = r.owes_reply_to_seq
           LEFT JOIN room_prefs rp ON rp.room_id = r.room_id AND rp.participant = r.participant
           WHERE r.room_id = ? ORDER BY r.participant""",
        (room_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["muted"] = bool(d["muted"])
        d["room_wake_allowed"] = bool(d["room_wake_allowed"])
        last_authored = d.pop("last_authored_ts")
        cursor_ts = d.pop("cursor_ts")
        d["last_active_ts"] = max(filter(None, [last_authored, cursor_ts]), default=None)
        result.append(d)
    return result
