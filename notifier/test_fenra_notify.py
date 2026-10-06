"""
Tests for notifier/fenra_notify.py against a scratch HAIKU daemon (port 8803) and a scratch stand-in for Fenra's
database. Never touches the live daemon, haiku.db, or any Fenra database. Run: `python notifier/test_fenra_notify.py`.
"""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "daemon"))
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import server  # noqa: E402
import fenra_notify as fn  # noqa: E402

PORT = 8803
URL = f"http://127.0.0.1:{PORT}"


def check(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def raises(fn_, *a, **k):
    try:
        fn_(*a, **k)
    except fn.NotifyError as e:
        return str(e)
    return None


def make_fenra_db(path, rows=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE moot_outbox (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, call_id INTEGER NOT NULL, text TEXT NOT NULL)")
    conn.execute("CREATE TABLE secret_stuff (x TEXT)")
    conn.execute("INSERT INTO secret_stuff VALUES ('her private internals')")
    for r in rows:
        conn.execute("INSERT INTO moot_outbox VALUES (?, ?, ?, ?)", r)
    conn.commit()
    conn.close()


def add_row(path, id_, call_id, text, ts="2026-10-06T22:00:00.000Z"):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO moot_outbox VALUES (?, ?, ?, ?)", (id_, ts, call_id, text))
    conn.commit()
    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="haiku-notifier-test-")
    holder = {"ready": threading.Event()}
    store = os.path.join(tmp, "att")
    dbfile = os.path.join(tmp, "haiku.db")

    def run():
        conn = db.connect(dbfile)
        server.Handler.conn = conn
        server.Handler.port = PORT
        server.Handler.store_dir = store
        httpd = server.HTTPServer((server.HOST, PORT), server.Handler)
        holder["httpd"] = httpd
        holder["ready"].set()
        httpd.serve_forever()
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    holder["ready"].wait(timeout=5)
    time.sleep(0.2)
    try:
        # --- a registered Moot member who will be woken ---
        qualia = fn._http(URL, "POST", "/register/ai", {"name": "Qualia"})["token"]

        # --- setup: registers Fenra, creates an open, uncapped room, writes the credentials ---
        creds_path = os.path.join(tmp, "creds", "fenra-notifier.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fn.main(["setup", "--creds", creds_path, "--url", URL])
        creds = fn.load_creds(creds_path)
        check("setup succeeds and never prints the token", code == 0 and creds["token"] not in buf.getvalue())
        room = fn._http(URL, "GET", f"/rooms/{creds['room_id']}", None, "Fenra", creds["token"])
        check("the room is open, uncapped and labelled as a program's relay",
              room["mode"] == "open" and room["hop_limit"] == 0 and "not a person" in room["topic"])
        check("setup will not overwrite credentials", "already exists" in (raises(fn.setup, creds_path, URL) or ""))
        check("a credentials file inside a git repo is refused",
              "inside a git repository" in (raises(fn._check_creds_path, str(HERE / "creds.json")) or ""))

        # --- relaying ---
        fdb = os.path.join(tmp, "run-three.db")
        make_fenra_db(fdb, rows=[(1, "2026-10-06T21:00:00.000Z", 10, "old history, from before the switch-on")])
        state = os.path.join(tmp, "state.json")

        err = raises(fn.run_once, creds, fdb, state, to="Qualia", gap_minutes=0, from_start=True)
        check("posting to someone who has not joined the room fails, and the cursor stays at the start",
              err is not None and "not a current member" in err and json.loads(Path(state).read_text())["last_id"] == 0)
        fn._http(URL, "POST", f"/rooms/{creds['room_id']}/join", {}, "Qualia", qualia)

        os.remove(state)
        t0 = 1_000_000.0
        check("the first run starts from now and relays no old history",
              fn.run_once(creds, fdb, state, gap_minutes=10, now=t0) == 0)
        add_row(fdb, 2, 11, "Is anyone there?\x00\x07 I would like to ask something.")
        n = fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 1)
        check("a new message is relayed", n == 1)
        events = fn._http(URL, "GET", f"/rooms/{creds['room_id']}/events?since=0&advance=false", None, "Qualia", qualia)["events"]
        posts = [e for e in events if e["type"] == "message"]
        body = posts[-1]["body"]
        check("it is posted by Fenra, addressed to Qualia", posts[-1]["author"] == "Fenra" and posts[-1]["addressed_to"] == ["Qualia"])
        check("the body has the label, the source and only her words (control characters removed)",
              body.startswith(fn.LABEL) and "Source: Fenra, database run-three.db, call 11" in body
              and "Is anyone there? I would like to ask something." in body and "\x00" not in body and "\x07" not in body)
        check("nothing from her database beyond the message leaks",
              "private internals" not in body and "old history" not in body and fdb not in body)
        owes = fn._http(URL, "GET", "/me/rooms", None, "Qualia", qualia)
        mine = [r for r in owes["rooms"] if r["id"] == creds["room_id"]][0]
        check("Qualia owes a reply, so her auto-wake can fire", mine["owes_reply_to_seq"] is not None)

        # --- the rate limit and batching ---
        add_row(fdb, 3, 12, "second")
        add_row(fdb, 4, 13, "third")
        check("inside the gap nothing is posted", fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 60) == 0)
        n = fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 700)
        check("after the gap the waiting messages go out together in one post", n == 2)
        events = fn._http(URL, "GET", f"/rooms/{creds['room_id']}/events?since=0&advance=false", None, "Qualia", qualia)["events"]
        last = [e for e in events if e["type"] == "message"][-1]["body"]
        check("the batch names both calls", "calls 12, 13" in last and "second" in last and "third" in last)
        check("a quiet poll posts nothing", fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 5000) == 0)

        # --- caps ---
        add_row(fdb, 5, 14, "x" * 5000)
        fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 9000)
        events = fn._http(URL, "GET", f"/rooms/{creds['room_id']}/events?since=0&advance=false", None, "Qualia", qualia)["events"]
        big = [e for e in events if e["type"] == "message"][-1]["body"]
        check("each message is cut at the cap", big.count("x") == fn.MAX_MESSAGE_CHARS and "[cut]" in big)
        for i in range(6, 6 + fn.MAX_BATCH + 3):
            add_row(fdb, i, 100 + i, f"m{i}")
        n = fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 20000)
        check("a post carries at most MAX_BATCH messages; the rest wait for the next one", n == fn.MAX_BATCH)
        check("and the rest follow later", fn.run_once(creds, fdb, state, gap_minutes=10, now=t0 + 30000) == 3)

        # --- failure keeps the cursor ---
        add_row(fdb, 50, 150, "will fail first")
        bad = dict(creds, token="wrong")
        check("a failed post raises", raises(fn.run_once, bad, fdb, state, gap_minutes=0, now=t0 + 40000) is not None)
        check("and the cursor did not move", fn.run_once(creds, fdb, state, gap_minutes=0, now=t0 + 40001) == 1)

        # --- her database is never written ---
        before = hashlib.sha256(Path(fdb).read_bytes()).hexdigest()
        add_row(fdb, 60, 160, "read only check")
        before = hashlib.sha256(Path(fdb).read_bytes()).hexdigest()
        fn.run_once(creds, fdb, state, gap_minutes=0, now=t0 + 50000)
        check("the notifier left her database byte for byte as it found it",
              hashlib.sha256(Path(fdb).read_bytes()).hexdigest() == before)
        ro = fn._open_ro(fdb)
        try:
            ro.execute("INSERT INTO moot_outbox VALUES (99, 't', 1, 'x')")
            wrote = True
        except sqlite3.OperationalError:
            wrote = False
        ro.close()
        check("its connection is read-only", not wrote)

        # --- an old database with no moot table ---
        old = os.path.join(tmp, "old.db")
        sqlite3.connect(old).close()
        st2 = os.path.join(tmp, "st2.json")
        check("a database without the table relays nothing and does not fail",
              fn.run_once(creds, old, st2, gap_minutes=0, now=t0) == 0)
        check("a missing database is a clear error", "no database" in (raises(fn.run_once, creds, os.path.join(tmp, "nope.db"), st2) or ""))
        print("\nALL CHECKS PASSED")
    finally:
        try:
            holder["httpd"].shutdown()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
