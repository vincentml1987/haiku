"""
Tests that db.connect()'s migration framework actually upgrades a
pre-existing db, not just a fresh one. test_db.py and test_server.py
always build fresh databases (db.connect() on a file that doesn't exist
yet), which structurally can never exercise a migration path — that's
exactly the class of bug that let 'archive' reach events.type's CHECK
constraint in code (8ae34f7) while the live db, created earlier, kept
rejecting it until manually migrated. This file builds a db from a
FROZEN COPY of the schema as it was at v1 (before 'archive' existed, and
before PRAGMA user_version was used at all — the real state of every db
that predates this system), then calls db.connect() and checks it
upgrades cleanly with no data lost.

Run directly: `python test_migrations.py`.
"""

import os
import sqlite3
import db

DBFILE = "test_migrations.db"

# Frozen: schema.sql as it was before 'archive' was added to events.type's
# CHECK constraint, and before PRAGMA user_version was adopted (so no
# version is set — the real shape of every pre-existing db). Do NOT update
# this to match schema.sql's current content; its entire point is staying
# exactly what v1 looked like.
SCHEMA_V1 = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS participants (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('human', 'ai')),
    address     TEXT,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_name_nocase
    ON participants(name COLLATE NOCASE);

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
    hop_limit   INTEGER NOT NULL DEFAULT 6,
    hop_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS events (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    author      TEXT NOT NULL REFERENCES participants(name),
    author_kind TEXT NOT NULL CHECK (author_kind IN ('human', 'ai')),
    type        TEXT NOT NULL CHECK (type IN
                    ('message', 'join', 'leave', 'topic_change',
                     'pass', 'pause', 'resume')),
    addressed_to TEXT,
    body        TEXT,
    PRIMARY KEY (room_id, seq)
);

CREATE TABLE IF NOT EXISTS roster (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    participant TEXT NOT NULL REFERENCES participants(name),
    status      TEXT NOT NULL DEFAULT 'present' CHECK (status IN ('present', 'away', 'left')),
    joined_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    owes_reply_to_seq INTEGER,
    PRIMARY KEY (room_id, participant)
);

CREATE TABLE IF NOT EXISTS cursors (
    room_id         TEXT NOT NULL REFERENCES rooms(id),
    participant     TEXT NOT NULL REFERENCES participants(name),
    last_delivered_seq INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (room_id, participant)
);

CREATE TABLE IF NOT EXISTS invites (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    participant TEXT NOT NULL REFERENCES participants(name),
    invited_by  TEXT NOT NULL REFERENCES participants(name),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (room_id, participant)
);

CREATE INDEX IF NOT EXISTS idx_events_room_seq ON events(room_id, seq);
CREATE INDEX IF NOT EXISTS idx_roster_room ON roster(room_id);
"""


def cleanup_files():
    # Best-effort: a connection db.connect() opened and then raised out of
    # (the refuse-newer-version case) isn't reachable here to close
    # explicitly, and Windows can hold a brief lock until it's GC'd.
    import gc
    gc.collect()
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        p = DBFILE + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except PermissionError:
                pass


def check(label, cond):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    cleanup_files()
    try:
        # Build a v1 db by hand (no db.connect() involved — that would run
        # the CURRENT schema.sql, defeating the point). user_version is
        # left at SQLite's default, 0: exactly what a real pre-versioning
        # db looks like.
        raw = sqlite3.connect(DBFILE)
        raw.executescript(SCHEMA_V1)
        raw.execute(
            "INSERT INTO participants (name, kind, token_hash) VALUES ('Teddy', 'human', 'x')"
        )
        raw.execute(
            "INSERT INTO rooms (id, name, created_by) VALUES ('r1', 'old-room', 'Teddy')"
        )
        raw.execute(
            """INSERT INTO events (room_id, seq, author, author_kind, type)
               VALUES ('r1', 1, 'Teddy', 'human', 'join')"""
        )
        raw.commit()
        raw.close()

        precheck = sqlite3.connect(DBFILE)
        check("user_version is 0 before migration (real pre-existing shape)",
              precheck.execute("PRAGMA user_version").fetchone()[0] == 0)
        precheck.close()

        # The actual thing under test: connect() must detect this is a
        # pre-existing, outdated db and migrate it, not just lay the new
        # schema over it and leave the old constraint in place.
        conn = db.connect(DBFILE)

        check("schema version after connect() is current",
              conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION)

        row_count = conn.execute("SELECT COUNT(*) FROM events WHERE room_id = 'r1'").fetchone()[0]
        check("the pre-existing event row survived the migration", row_count == 1)
        surviving = conn.execute(
            "SELECT author, type FROM events WHERE room_id='r1' AND seq=1"
        ).fetchone()
        check("the surviving row's data is intact",
              tuple(surviving) == ("Teddy", "join"))

        # The actual bug this whole framework exists to prevent: archive
        # must now work, where it would have raised IntegrityError before.
        token = db.register_human(conn, "AdminTest", db._admin_secret_path(DBFILE).read_text().strip())
        db.archive_room(conn, "r1", "AdminTest", token)
        check("archive works after migration (the bug this prevents)",
              dict(db._get_room(conn, "r1"))["state"] == "archived")

        # The lobby needs no schema change; connect() must still give a
        # pre-lobby db one, with its existing human ('Teddy') as a member.
        lobby = db._lobby_row(conn)
        check("a pre-lobby db gets a lobby on connect()", lobby is not None)
        check("the existing human is a lobby member after migration",
              conn.execute("SELECT status FROM roster WHERE room_id = ? AND participant = 'Teddy'",
                           (lobby["id"],)).fetchone()["status"] == "present")

        check("a backup file was made before migrating", os.path.exists(f"{DBFILE}.backup-before-migration-v1-to-v{db.CURRENT_SCHEMA_VERSION}"))

        conn.close()

        # Re-opening an already-current db must be a no-op, not re-migrate
        # or re-backup.
        conn2 = db.connect(DBFILE)
        check("re-opening an up-to-date db stays at the current version",
              conn2.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION)
        conn2.close()

        # A db claiming a NEWER version than this code knows must be refused.
        conn3 = sqlite3.connect(DBFILE)
        conn3.execute(f"PRAGMA user_version = {db.CURRENT_SCHEMA_VERSION + 1}")
        conn3.close()
        try:
            db.connect(DBFILE)
            check("a newer-than-known schema version is refused", False)
        except RuntimeError:
            check("a newer-than-known schema version is refused", True)

        print("\nALL CHECKS PASSED")
    finally:
        cleanup_files()
        for f in os.listdir("."):
            if f.startswith(DBFILE + ".backup-before-migration"):
                try:
                    os.remove(f)
                except PermissionError:
                    pass


if __name__ == "__main__":
    main()
