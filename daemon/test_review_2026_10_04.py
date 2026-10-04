"""
Independent review tests for the 2026-10-04 update (commit 3f88169): admin
join, archive rename, per-room mute, per-room wake, schema v4 migration.
Written by Tessera as reviewer, separate from the builder's test_db.py so the
two cannot mask each other. Run directly: `python test_review_2026_10_04.py`.

Uses a private temp directory, never test_haiku.db or the live haiku.db.
"""

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

import db


def check(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


def owes(conn, room, who):
    return conn.execute(
        "SELECT owes_reply_to_seq FROM roster WHERE room_id=? AND participant=?", (room, who)
    ).fetchone()["owes_reply_to_seq"]


def room_name(conn, room):
    return conn.execute("SELECT name FROM rooms WHERE id=?", (room,)).fetchone()["name"]


def main():
    tmp = tempfile.mkdtemp(prefix="haiku-review-")
    path = os.path.join(tmp, "review.db")
    conn = db.connect(path)
    try:
        secret = db._admin_secret_path(path).read_text().strip()
        t = db.register_human(conn, "Teddy", secret)
        a = db.register_ai(conn, "Ann")
        b = db.register_ai(conn, "Bo")
        c = db.register_ai(conn, "Cy")

        # ---------- fresh db lands on the current schema, room_prefs present
        check("fresh db is at CURRENT_SCHEMA_VERSION",
              conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION)
        check("room_prefs table exists on a fresh db",
              conn.execute("SELECT 1 FROM sqlite_master WHERE name='room_prefs'").fetchone() is not None)

        # ---------- item 1: admin join -------------------------------------
        closed = db.create_room(conn, "secret-room", "Ann", a, mode="closed")
        check("AI non-member, uninvited, cannot join a closed room",
              raises(db.HaikuError, db.join_room, conn, closed, "Bo", b))
        check("AI cannot read a closed room it never joined",
              raises(db.Forbidden, db.read_events, conn, closed, "Bo", b))
        db.join_room(conn, closed, "Teddy", t)
        check("human joins a closed room uninvited", db._is_active_member(conn, closed, "Teddy"))
        ev = conn.execute("SELECT type, author FROM events WHERE room_id=? ORDER BY seq", (closed,)).fetchall()
        check("the admin join is a visible join event (no silent lurking)",
              any(e["type"] == "join" and e["author"] == "Teddy" for e in ev))
        check("admin power is not inherited by AIs after the human joined",
              raises(db.HaikuError, db.join_room, conn, closed, "Cy", c))
        check("wrong token does not get the admin bypass",
              raises(db.HaikuError, db.join_room, conn, closed, "Teddy", b))

        # ---------- item 2: archive rename ---------------------------------
        r1 = db.create_room(conn, "plans", "Ann", a, mode="open")
        new = db.archive_room(conn, r1, "Teddy", t)
        check("archive returns the new name and stores it", room_name(conn, r1) == new)
        check("new name is <old>-A + 14 digits",
              new.startswith("plans-A") and new[len("plans-A"):].isdigit() and len(new) == len("plans-A") + 14)
        check("room id unchanged by the rename",
              conn.execute("SELECT 1 FROM rooms WHERE id=?", (r1,)).fetchone() is not None)
        check("archive event records the old name",
              "plans" in conn.execute(
                  "SELECT body FROM events WHERE room_id=? AND type='archive'", (r1,)).fetchone()["body"])
        check("re-archiving is refused, so suffixes cannot stack",
              raises(db.HaikuError, db.archive_room, conn, r1, "Teddy", t))
        check("an AI cannot archive", raises(db.HaikuError, db.archive_room, conn, db.create_room(
            conn, "x1", "Ann", a, mode="open"), "Ann", a))

        # collision within the same second: force the same timestamp
        when = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
        n1 = db._archived_name(conn, "dup", when)
        conn.execute("INSERT INTO rooms (id, name, created_by, mode) VALUES ('id-dup1', ?, 'Ann', 'open')", (n1,))
        conn.commit()
        n2 = db._archived_name(conn, "dup", when)
        check("same-second collision gets a -2 suffix, not an IntegrityError", n2 == n1 + "-2" and n2 != n1)
        conn.execute("INSERT INTO rooms (id, name, created_by, mode) VALUES ('id-dup2', ?, 'Ann', 'open')", (n2,))
        conn.commit()
        check("third collision gets -3", db._archived_name(conn, "dup", when) == n1 + "-3")

        long_base = "L" * 64
        nl = db._archived_name(conn, long_base, when)
        check("a 64-char name still fits the 64-char cap after the suffix", len(nl) <= db.NAME_MAX_LEN)
        check("the suffix survives the trim", nl.endswith("-A20261004120000"))
        # collision on a trimmed name too
        conn.execute("INSERT INTO rooms (id, name, created_by, mode) VALUES ('id-long', ?, 'Ann', 'open')", (nl,))
        conn.commit()
        nl2 = db._archived_name(conn, long_base, when)
        check("collision on a trimmed name stays within the cap and unique",
              len(nl2) <= db.NAME_MAX_LEN and nl2 != nl)
        check("lobby still cannot be archived",
              raises(db.HaikuError, db.archive_room, conn,
                     conn.execute("SELECT id FROM rooms WHERE name='lobby'").fetchone()["id"], "Teddy", t))
        check("archive stamp clock flag exists (UTC unless Teddy's local-time call is applied)",
              isinstance(db.ARCHIVE_STAMP_UTC, bool))

        # ---------- item 5: mute -------------------------------------------
        room = db.create_room(conn, "work", "Ann", a, mode="open")
        for who, tok in (("Bo", b), ("Cy", c), ("Teddy", t)):
            db.join_room(conn, room, who, tok)
        outsider = db.register_ai(conn, "Out")
        check("non-member cannot mute", raises(db.Forbidden, db.set_room_muted, conn, room, "Out", outsider, True))
        check("a participant cannot mute on someone else's behalf (token mismatch refused)",
              raises(db.HaikuError, db.set_room_muted, conn, room, "Bo", a, True))

        db.set_room_muted(conn, room, "Bo", b, True)
        row = conn.execute("SELECT muted FROM room_prefs WHERE room_id=? AND participant='Bo'", (room,)).fetchone()
        check("mute is stored for Bo only", row["muted"] == 1 and conn.execute(
            "SELECT COUNT(*) n FROM room_prefs WHERE room_id=? AND muted=1", (room,)).fetchone()["n"] == 1)

        s = db.send_message(conn, room, "Teddy", t, "unaddressed hello")["seq"]
        check("unaddressed human message: muted Bo not obligated", owes(conn, room, "Bo") is None)
        check("unaddressed human message: unmuted Cy obligated", owes(conn, room, "Cy") == s)

        s = db.send_message(conn, room, "Teddy", t, "all hands", addressed_to=["all"])["seq"]
        check('["all"] human message: muted Bo not obligated', owes(conn, room, "Bo") is None)

        s = db.send_message(conn, room, "Teddy", t, "Bo, you specifically", addressed_to=["Bo"])["seq"]
        check("human message addressed to muted Bo DOES obligate (Teddy's decision, 2026-10-04)",
              owes(conn, room, "Bo") == s)

        s2 = db.send_message(conn, room, "Teddy", t, "Cy only", addressed_to=["Cy"])["seq"]
        check("KNOWN LIMIT: a later human message to someone else clears the muted AI's breakthrough "
              "obligation before it may have been read (plugin should key on events, not the flag)",
              owes(conn, room, "Bo") is None)

        s = db.send_message(conn, room, "Cy", c, "Bo, ping", addressed_to=["Bo"])["seq"]
        check("AI-addressed message does not break through a mute", owes(conn, room, "Bo") is None)

        db.send_message(conn, room, "Teddy", t, "Bo again", addressed_to=["Bo"])
        db.set_room_muted(conn, room, "Bo", b, True)
        check("muting clears an obligation already owed", owes(conn, room, "Bo") is None)

        check("muted AI can still read the room on demand",
              len(db.read_events(conn, room, "Bo", b, since=0)["events"]) > 0)
        mine = {r["id"]: r for r in db.list_my_rooms(conn, "Bo", b)}
        check("list_my_rooms reports muted and room_wake_allowed",
              mine[room]["muted"] is True and mine[room]["room_wake_allowed"] is True)
        db.set_room_muted(conn, room, "Bo", b, False)
        s = db.send_message(conn, room, "Teddy", t, "after unmute")["seq"]
        check("unmuting restores obligations", owes(conn, room, "Bo") == s)
        check("mute row is per room: another room is unaffected",
              not db._is_muted(conn, closed, "Bo"))

        # ---------- item 6: per-room wake ----------------------------------
        check("an AI cannot set a room's wake flag",
              raises(db.Forbidden, db.set_room_wake_allowed, conn, "Ann", a, room, "Bo", True))
        check("an AI cannot raise its own room wake flag after a human restricted it",
              (db.set_room_wake_allowed(conn, "Teddy", t, room, "Bo", False),
               raises(db.Forbidden, db.set_room_wake_allowed, conn, "Bo", b, room, "Bo", True))[1])
        check("human restriction is stored per room, per AI",
              {r["participant"]: r["room_wake_allowed"] for r in db.room_roster(conn, room)}["Bo"] is False)
        check("other AIs in that room default to allowed",
              {r["participant"]: r["room_wake_allowed"] for r in db.room_roster(conn, room)}["Cy"] is True)
        check("other rooms default to allowed for that AI",
              {r["id"]: r for r in db.list_my_rooms(conn, "Bo", b)}.get(closed, {"room_wake_allowed": True})["room_wake_allowed"] is True)
        check("leaving and rejoining does not reset the human's restriction",
              (db.leave_room(conn, room, "Bo", b), db.join_room(conn, room, "Bo", b),
               {r["participant"]: r["room_wake_allowed"] for r in db.room_roster(conn, room)}["Bo"])[2] is False)
        check("setting wake for an unknown participant is refused",
              raises(db.HaikuError, db.set_room_wake_allowed, conn, "Teddy", t, room, "Nobody", False))
        check("setting wake for an unknown room is refused",
              raises(db.HaikuError, db.set_room_wake_allowed, conn, "Teddy", t, "no-such-room", "Bo", False))
        check("global wake switch still works and is independent",
              (db.set_wake_allowed(conn, "Teddy", t, "Bo", False), db.get_wake_allowed(conn, "Bo"))[1] is False)
        check("an AI still cannot touch the global switch",
              raises(db.Forbidden, db.set_wake_allowed, conn, "Bo", b, "Bo", True))
        db.set_room_wake_allowed(conn, "Teddy", t, room, "Bo", True)
        check("human can lift the restriction",
              {r["participant"]: r["room_wake_allowed"] for r in db.room_roster(conn, room)}["Bo"] is True)

        print("\nall review checks passed")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    migration_check()


def migration_check():
    """A v3 database (no room_prefs) upgrades to v4 with data intact and is
    refused by older code afterwards. Built from the CURRENT schema.sql minus
    room_prefs, stamped user_version=3, then opened through db.connect()."""
    tmp = tempfile.mkdtemp(prefix="haiku-review-mig-")
    path = os.path.join(tmp, "v3.db")
    try:
        sql = db.SCHEMA_PATH.read_text()
        start = sql.index("-- Per-room, per-participant preferences")
        end = sql.index("CREATE INDEX IF NOT EXISTS idx_events_room_seq")
        v3_sql = sql[:start] + sql[end:]
        raw = sqlite3.connect(path)
        raw.executescript(v3_sql)
        raw.execute("INSERT INTO participants (name, kind, token_hash) VALUES ('Old', 'ai', 'x')")
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
        check("test setup: v3 db has no room_prefs",
              raw.execute("SELECT 1 FROM sqlite_master WHERE name='room_prefs'").fetchone() is None)
        raw.close()

        conn = db.connect(path)
        check("v3 db upgrades to the current version",
              conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION)
        check("room_prefs exists after upgrade",
              conn.execute("SELECT 1 FROM sqlite_master WHERE name='room_prefs'").fetchone() is not None)
        check("existing participants survive the upgrade",
              conn.execute("SELECT 1 FROM participants WHERE name='Old'").fetchone() is not None)
        conn.close()
        conn = db.connect(path)  # second open must be a no-op
        check("reopening an upgraded db is a clean no-op",
              conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION)
        conn.execute("PRAGMA user_version = %d" % (db.CURRENT_SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        check("a db newer than the code is refused", raises(RuntimeError, db.connect, path))
        print("migration checks passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
