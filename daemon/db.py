"""
HAIKU daemon core: room/event operations over the SQLite schema.

Implements docs/haiku-room-spec.md. This module is the only place that
touches obligation/hop-cap/cursor logic directly — the HTTP layer (server.py)
should just translate requests into these calls.
"""

import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class HaikuError(Exception):
    """Raised for rule violations the caller should see as a clear result,
    not a stack trace (e.g. room paused, unknown room)."""


def ensure_participant(conn, name: str, kind: str, address: str | None = None):
    if kind not in ("human", "ai"):
        raise HaikuError(f"invalid participant kind: {kind}")
    conn.execute(
        """INSERT INTO participants (name, kind, address) VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET address = excluded.address""",
        (name, kind, address),
    )
    conn.commit()


def create_room(conn, name: str, created_by: str, topic: str | None = None,
                 mode: str = "closed", hop_limit: int = 6) -> str:
    room_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO rooms (id, name, topic, created_by, mode, hop_limit)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (room_id, name, topic, created_by, mode, hop_limit),
    )
    conn.commit()
    _join(conn, room_id, created_by)
    return room_id


def _get_room(conn, room_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise HaikuError(f"no such room: {room_id}")
    return row


def _next_seq(conn, room_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE room_id = ?",
        (room_id,),
    ).fetchone()
    return row["n"]


def _insert_event(conn, room_id, author, author_kind, type_, addressed_to=None, body=None) -> int:
    seq = _next_seq(conn, room_id)
    conn.execute(
        """INSERT INTO events (room_id, seq, ts, author, author_kind, type, addressed_to, body)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (room_id, seq, _now(), author, author_kind, type_, addressed_to, body),
    )
    return seq


def _join(conn, room_id, participant):
    row = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (participant,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {participant}")
    seq = _insert_event(conn, room_id, participant, row["kind"], "join")
    conn.execute(
        """INSERT INTO roster (room_id, participant, status, joined_at)
           VALUES (?, ?, 'present', ?)
           ON CONFLICT(room_id, participant)
           DO UPDATE SET status = 'present'""",
        (room_id, participant, _now()),
    )
    conn.execute(
        """INSERT INTO cursors (room_id, participant, last_delivered_seq)
           VALUES (?, ?, 0)
           ON CONFLICT(room_id, participant) DO NOTHING""",
        (room_id, participant),
    )
    conn.commit()
    return seq


def join_room(conn, room_id: str, participant: str) -> int:
    """Explicit join, per spec §2 — a session joins itself, or accepts an
    invite. This function does not itself check invites; that's a policy
    decision for the HTTP layer / Teddy's view, not the data model."""
    _get_room(conn, room_id)
    return _join(conn, room_id, participant)


def leave_room(conn, room_id: str, participant: str) -> int:
    _get_room(conn, room_id)
    row = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (participant,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {participant}")
    seq = _insert_event(conn, room_id, participant, row["kind"], "leave")
    conn.execute(
        "UPDATE roster SET status = 'left' WHERE room_id = ? AND participant = ?",
        (room_id, participant),
    )
    conn.commit()
    return seq


def mark_away(conn, room_id: str, participant: str):
    """A session that stops running becomes 'away', not 'left' (spec §2).
    No event is logged — presence state only, not a room action."""
    conn.execute(
        """UPDATE roster SET status = 'away'
           WHERE room_id = ? AND participant = ? AND status = 'present'""",
        (room_id, participant),
    )
    conn.commit()


def _present_ai_participants(conn, room_id: str, exclude: str | None = None) -> list[str]:
    rows = conn.execute(
        """SELECT r.participant FROM roster r
           JOIN participants p ON p.name = r.participant
           WHERE r.room_id = ? AND r.status != 'left' AND p.kind = 'ai'
             AND r.participant != COALESCE(?, '')""",
        (room_id, exclude),
    ).fetchall()
    return [r["participant"] for r in rows]


def _set_obligation(conn, room_id: str, participant: str, seq: int | None):
    conn.execute(
        """UPDATE roster SET owes_reply_to_seq = ?
           WHERE room_id = ? AND participant = ?""",
        (seq, room_id, participant),
    )


def send_message(conn, room_id: str, author: str, body: str,
                  addressed_to: list[str] | None = None) -> dict:
    """addressed_to: None (unaddressed), ['all'], or a list of participant
    names. Implements spec §3 turn-taking and §3.3 hop cap."""
    room = _get_room(conn, room_id)
    row = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (author,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {author}")
    author_kind = row["kind"]

    if room["state"] == "paused" and author_kind == "ai":
        raise HaikuError("room paused, waiting on Teddy")

    addressed_str = ",".join(addressed_to) if addressed_to else None
    seq = _insert_event(conn, room_id, author, author_kind, "message",
                         addressed_to=addressed_str, body=body)

    new_hop_count = room["hop_count"]
    new_state = room["state"]

    if author_kind == "human":
        new_hop_count = 0
        if addressed_to and addressed_to != ["all"]:
            for name in addressed_to:
                _set_obligation(conn, room_id, name, seq)
        else:
            for name in _present_ai_participants(conn, room_id, exclude=author):
                _set_obligation(conn, room_id, name, seq)
    else:
        _set_obligation(conn, room_id, author, None)  # this reply discharges the author's own obligation
        if addressed_to and addressed_to != ["all"]:
            for name in addressed_to:
                if name != author:
                    _set_obligation(conn, room_id, name, seq)
        new_hop_count = room["hop_count"] + 1
        if new_hop_count >= room["hop_limit"]:
            new_state = "paused"
            _insert_event(conn, room_id, author, author_kind, "pause",
                          body=f"hop cap ({room['hop_limit']}) reached")

    conn.execute(
        "UPDATE rooms SET hop_count = ?, state = ? WHERE id = ?",
        (new_hop_count, new_state, room_id),
    )
    conn.commit()
    return {"seq": seq, "room_state": new_state}


def send_pass(conn, room_id: str, author: str) -> int:
    room = _get_room(conn, room_id)
    row = conn.execute(
        "SELECT kind FROM participants WHERE name = ?", (author,)
    ).fetchone()
    if row is None:
        raise HaikuError(f"unknown participant: {author}")
    if room["state"] == "paused" and row["kind"] == "ai":
        raise HaikuError("room paused, waiting on Teddy")
    seq = _insert_event(conn, room_id, author, row["kind"], "pass")
    _set_obligation(conn, room_id, author, None)
    conn.commit()
    return seq


def resume_room(conn, room_id: str, granted_hops: int | None = None) -> None:
    """Only Teddy should call this in practice (spec §3.3: 'continue' /
    'continue N'); not enforced here, that's an HTTP-layer/caller policy."""
    room = _get_room(conn, room_id)
    new_limit = room["hop_count"] + (granted_hops or room["hop_limit"])
    conn.execute(
        "UPDATE rooms SET state = 'active', hop_limit = ? WHERE id = ?",
        (new_limit, room_id),
    )
    conn.execute(
        """INSERT INTO events (room_id, seq, ts, author, author_kind, type, body)
           SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, 'Teddy', 'human', 'resume', ?
           FROM events WHERE room_id = ?""",
        (room_id, _now(), f"resumed, hop_limit now {new_limit}", room_id),
    )
    conn.commit()


def read_events(conn, room_id: str, participant: str, since: int | None = None) -> list[dict]:
    """Returns events after `since` (explicit pull) or after the stored
    cursor (default catch-up), and ADVANCES the cursor to the last seq
    returned — delivery, not mere queuing, per spec §2."""
    _get_room(conn, room_id)
    if since is None:
        cur = conn.execute(
            "SELECT last_delivered_seq FROM cursors WHERE room_id = ? AND participant = ?",
            (room_id, participant),
        ).fetchone()
        since = cur["last_delivered_seq"] if cur else 0

    rows = conn.execute(
        """SELECT seq, ts, author, author_kind, type, addressed_to, body
           FROM events WHERE room_id = ? AND seq > ? ORDER BY seq ASC""",
        (room_id, since),
    ).fetchall()

    if rows:
        max_seq = rows[-1]["seq"]
        conn.execute(
            """INSERT INTO cursors (room_id, participant, last_delivered_seq, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(room_id, participant)
               DO UPDATE SET last_delivered_seq = excluded.last_delivered_seq,
                              updated_at = excluded.updated_at""",
            (room_id, participant, max_seq, _now()),
        )
        conn.commit()

    return [dict(r) for r in rows]


def room_roster(conn, room_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT participant, status, owes_reply_to_seq
           FROM roster WHERE room_id = ? ORDER BY participant""",
        (room_id,),
    ).fetchall()
    return [dict(r) for r in rows]
