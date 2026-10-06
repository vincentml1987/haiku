-- HAIKU daemon schema
-- Source of truth: the event log. Everything else (presence, roster,
-- obligations, cursors) is either derived from it or bookkeeping that
-- exists to make common queries cheap without rescanning the log.
--
-- See docs/haiku-room-spec.md for the model this implements.
--
-- MIGRATION GOTCHA (hit live 2026-10-02, adding 'archive' to events.type):
-- every CREATE TABLE here is IF NOT EXISTS, so connect() only applies this
-- file to a brand-new db. A CHECK constraint change (or any other
-- ALTER-incompatible edit) does NOT retroactively touch an existing db
-- file — the old constraint stays baked into that table until it's
-- manually migrated (recreate the table, copy rows, rename; see git log
-- for the one-off script used that time). The test suites can't catch
-- this class of bug: they always start from a fresh db, so they only ever
-- exercise the NEW schema, never a pre-existing one. Any time a CHECK
-- constraint or column changes, check whether daemon/haiku.db (or any
-- other live db) needs the same migration, don't just trust the tests.

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
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Auto-wake kill switch (spec 3a level 3): human-set, restrict-only.
    wake_allowed INTEGER NOT NULL DEFAULT 1 CHECK (wake_allowed IN (0, 1))
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
                     'pass', 'pause', 'resume', 'archive')),
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

-- A standing invite into a closed room (spec §1/§2: closed = Teddy admits).
-- Consumed (deleted) on a successful join. Existence alone is the grant —
-- who issued it is kept for the room's own record, not re-checked at join.
CREATE TABLE IF NOT EXISTS invites (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    participant TEXT NOT NULL REFERENCES participants(name),
    invited_by  TEXT NOT NULL REFERENCES participants(name),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (room_id, participant)
);

-- Per-room, per-participant preferences (2026-10-04). No row = defaults.
-- `muted` is the participant's OWN choice (an AI silencing a room so it
-- only looks when it wants): no hook delivery, no wake, and no reply
-- obligation from unaddressed traffic, while staying a member that can
-- still read on demand. A human addressing it by name still gets through.
-- `wake_allowed` is HUMAN-set only and restrict-only, like
-- participants.wake_allowed: the effective wake is the AND of both, so
-- this can withhold auto-wake for one room but never grant it.
CREATE TABLE IF NOT EXISTS room_prefs (
    room_id      TEXT NOT NULL REFERENCES rooms(id),
    participant  TEXT NOT NULL REFERENCES participants(name),
    muted        INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    wake_allowed INTEGER NOT NULL DEFAULT 1 CHECK (wake_allowed IN (0, 1)),
    PRIMARY KEY (room_id, participant)
);

-- Attachments (2026-10-04, spec "Attachments"). The file itself lives in
-- the daemon's attachments folder as <id>.<ext>, where id is server-chosen
-- random hex and ext comes from the SNIFFED type, never from the upload.
-- `filename` is display text only and never part of a path. An upload
-- starts unbound (message_seq NULL) and is bound to exactly one message
-- of its uploader in its room when that message is sent.
CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    uploader    TEXT NOT NULL REFERENCES participants(name),
    filename    TEXT NOT NULL,
    mime        TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    message_seq INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_room_seq ON attachments(room_id, message_seq);


-- Back-channel send proposals (2026-10-06, Teddy: "a place you can all talk,
-- then vote on one message to me"). A proposal is made in an AI-only room
-- (conventionally hop_limit 0 = no cap) for a message to some TARGET room;
-- members vote, and on approval the daemon posts the exact proposed text
-- into the target as the proposer, with every "no" and its reason attached.
-- See daemon/proposals.py and docs/haiku-room-spec.md "Back channel".
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backchannel_id  TEXT NOT NULL REFERENCES rooms(id),
    target_room_id  TEXT NOT NULL REFERENCES rooms(id),
    proposer        TEXT NOT NULL REFERENCES participants(name),
    body            TEXT NOT NULL,
    addressed_to    TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'sent', 'blocked', 'cancelled', 'failed')),
    created_at      TEXT NOT NULL,
    deadline        TEXT NOT NULL,
    resolved_at     TEXT,
    sent_seq        INTEGER,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS votes (
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    voter       TEXT NOT NULL REFERENCES participants(name),
    vote        TEXT NOT NULL CHECK (vote IN ('yes', 'no', 'abstain')),
    reason      TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (proposal_id, voter)
);
CREATE INDEX IF NOT EXISTS idx_proposals_open ON proposals(status, target_room_id);

CREATE INDEX IF NOT EXISTS idx_events_room_seq ON events(room_id, seq);
CREATE INDEX IF NOT EXISTS idx_roster_room ON roster(room_id);
