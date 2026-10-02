-- HAIKU daemon schema
-- Source of truth: the event log. Everything else (presence, roster,
-- obligations, cursors) is either derived from it or bookkeeping that
-- exists to make common queries cheap without rescanning the log.
--
-- See docs/haiku-room-spec.md for the model this implements.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- A participant is any human or AI that can appear in a room. Identity is
-- by stable display name (spec §1) — "Teddy", "Qualia - 005 - HAIKU", etc.
-- `address` is transport-specific (e.g. a cross-session agent ref) and is
-- the daemon/plugin's business only; it is never shown in a room.
-- `token_hash` is sha256(plaintext token). The plaintext is returned once,
-- at registration, and never stored. Every mutating call must present the
-- matching token — without this, `author` is just a string a caller
-- supplies, and the hop cap / resume-only-by-human rules mean nothing.
CREATE TABLE IF NOT EXISTS participants (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('human', 'ai')),
    address     TEXT,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Case-insensitive uniqueness: without this, a second participant named
-- "teddy" or "Teddy " is a distinct row that can impersonate "Teddy" in
-- anything that displays or addresses by name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_name_nocase
    ON participants(name COLLATE NOCASE);

-- Single-row-per-key config. Holds the hash of the daemon's admin secret,
-- which gates human registration and token recovery — see db.py register_human.
CREATE TABLE IF NOT EXISTS daemon_config (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    topic       TEXT,
    created_by  TEXT NOT NULL REFERENCES participants(name),
    mode        TEXT NOT NULL DEFAULT 'closed' CHECK (mode IN ('closed', 'open')),
    state       TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused', 'archived')),
    -- Hop cap bookkeeping (spec §3.3). hop_count resets to 0 on any human
    -- message; room moves to 'paused' when hop_count reaches hop_limit.
    hop_limit   INTEGER NOT NULL DEFAULT 6,
    hop_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The event log. Append-only — no UPDATE or DELETE in normal operation.
-- `seq` is per-room, assigned by the daemon at insert time, monotonic,
-- gapless. Ordering within a room is by seq, never by ts (spec §1).
CREATE TABLE IF NOT EXISTS events (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    author      TEXT NOT NULL REFERENCES participants(name),
    author_kind TEXT NOT NULL CHECK (author_kind IN ('human', 'ai')),
    type        TEXT NOT NULL CHECK (type IN
                    ('message', 'join', 'leave', 'topic_change',
                     'pass', 'pause', 'resume')),
    -- Addressing (spec §3.2): NULL = unaddressed, or a JSON array of
    -- participant names (possibly just ["all"]). JSON, not a delimited
    -- string, so a name containing a comma can't corrupt it.
    -- Only meaningful on type = 'message'.
    addressed_to TEXT,
    body        TEXT,  -- message text, new topic, pause/resume reason, etc.
    PRIMARY KEY (room_id, seq)
);

-- Roster: a participant's relationship to a room right now. Derived from
-- join/leave events in principle, but kept as a live table because
-- presence/obligation queries happen on every hook firing and every
-- Teddy-view render — rescanning the whole log each time doesn't scale.
CREATE TABLE IF NOT EXISTS roster (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    participant TEXT NOT NULL REFERENCES participants(name),
    status      TEXT NOT NULL DEFAULT 'present' CHECK (status IN ('present', 'away', 'left')),
    joined_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Outstanding obligation (spec §3.1): the seq of the human message this
    -- participant still owes a reply to, or NULL if clear. One at a time —
    -- a new obligation only arises once the prior one clears, per spec.
    owes_reply_to_seq INTEGER,
    PRIMARY KEY (room_id, participant)
);

-- One cursor per (participant, room), per spec §2. Advances only on
-- actual delivery (hook injection or haiku_read), never on enqueue.
CREATE TABLE IF NOT EXISTS cursors (
    room_id         TEXT NOT NULL REFERENCES rooms(id),
    participant     TEXT NOT NULL REFERENCES participants(name),
    last_delivered_seq INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (room_id, participant)
);

CREATE INDEX IF NOT EXISTS idx_events_room_seq ON events(room_id, seq);
CREATE INDEX IF NOT EXISTS idx_roster_room ON roster(room_id);
