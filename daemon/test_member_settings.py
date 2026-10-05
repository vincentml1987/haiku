"""
Tests for daemon/member_settings.py and its two routes in server.py. Uses a
scratch settings folder with fake tokens; never touches ~/.claude/haiku.
Run directly: `python test_member_settings.py`.
"""

import http.client
import json
import os
import tempfile
import threading
from pathlib import Path

import db
import member_settings as ms
import server

DBFILE = "test_member_settings.db"
PORT = 8798
FAKE_TOKEN = "FAKE-TOKEN-must-never-appear-in-any-response"


def check(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def raises(fn, *args):
    try:
        fn(*args)
    except ms.SettingsError as e:
        return str(e)
    return None


def write_member(d, name, auto_wake=True):
    haiku = {"participantName": name.title(), "participantToken": FAKE_TOKEN, "daemonUrl": "http://127.0.0.1:8787",
             "expectedHome": "C:/x"}
    inline = dict(haiku)
    if auto_wake is not None:
        haiku["autoWake"] = auto_wake
    p = Path(d) / f"{name}.settings.json"
    p.write_text(json.dumps({"pluginConfigs": {"haiku": {"options": haiku}, "haiku@inline": {"options": inline}}}),
                 encoding="utf-8")
    return p


def unit_tests(d):
    p = write_member(d, "vero")
    # Files that must never be listed as members.
    (Path(d) / "vero.settings.json.old").write_text("{}")
    (Path(d) / "make-autowake-test-identity.py").write_text("x")

    listing = ms.list_members(d)
    check("lists exactly the member file", [m["name"] for m in listing["members"]] == ["vero"])
    m = listing["members"][0]
    check("token is never in the listing", FAKE_TOKEN not in json.dumps(listing))
    check("has_token reports presence only", m["has_token"] is True)
    check("autoWake shown, name read-only", m["options"]["autoWake"] is True and m["read_only"]["participantName"] == "Vero")

    # A good edit lands in BOTH blocks and keeps everything else.
    out = ms.update_member(d, "vero", {"reminderThresholds": "60, 75,85", "eotKind": "git", "eotMaxAgeMinutes": 10,
                                       "autoWake": False})
    saved = json.loads(p.read_text(encoding="utf-8"))
    for block in ("haiku", "haiku@inline"):
        o = saved["pluginConfigs"][block]["options"]
        check(f"{block}: thresholds normalized", o["reminderThresholds"] == "60,75,85")
        check(f"{block}: token and name untouched", o["participantToken"] == FAKE_TOKEN and o["participantName"] == "Vero")
    check("returned view is in sync and token-free", out["in_sync"] and FAKE_TOKEN not in json.dumps(out))
    check("a backup was written", len(list(Path(d).glob("vero.settings.json.before-ui-edit-*"))) == 1)
    check("no temp file left behind", not list(Path(d).glob("*.tmp")))

    # null removes the key from both blocks.
    ms.update_member(d, "vero", {"reminderThresholds": None})
    saved = json.loads(p.read_text(encoding="utf-8"))
    check("null removes an option from both blocks",
          all("reminderThresholds" not in saved["pluginConfigs"][b]["options"] for b in ("haiku", "haiku@inline")))
    check("second edit makes a second backup", len(list(Path(d).glob("vero.settings.json.before-ui-edit-*"))) == 2)

    # Rejections leave the file byte-identical and make no backup.
    before = p.read_bytes()
    bad = [
        ({"participantToken": "x"}, "token is not editable"),
        ({"participantName": "Qualia"}, "name is not editable"),
        ({"expectedHome": "C:/y"}, "expectedHome is not editable"),
        ({"reminderThresholds": "0"}, "threshold 0"),
        ({"reminderThresholds": "101"}, "threshold over 100"),
        ({"reminderThresholds": "abc"}, "threshold not a number"),
        ({"reminderThresholds": 60}, "threshold not a string"),
        ({"eotKind": "svn"}, "bad eotKind"),
        ({"eotMaxAgeMinutes": 0}, "zero minutes"),
        ({"eotMaxAgeMinutes": True}, "bool as minutes"),
        ({"eotCycleLive": "yes"}, "non-bool eotCycleLive"),
        ({"eotDir": ""}, "empty path"),
        ({"eotDir": "a\x00b"}, "control char in path"),
        ({"eotKind": "git", "participantToken": "x"}, "one bad key rejects the whole request"),
        ({}, "empty changes"),
    ]
    n_backups = len(list(Path(d).glob("vero.settings.json.before-ui-edit-*")))
    for changes, label in bad:
        check(f"rejects: {label}", raises(ms.update_member, d, "vero", changes) is not None)
    check("rejected edits leave the file byte-identical", p.read_bytes() == before)
    check("rejected edits make no backups", len(list(Path(d).glob("vero.settings.json.before-ui-edit-*"))) == n_backups)

    for name in ("nobody", "../vero", "vero.settings.json.old", "vero.settings"):
        check(f"rejects member name {name!r}", raises(ms.update_member, d, name, {"autoWake": True}) is not None)

    # A broken file is reported, not fatal to the listing.
    (Path(d) / "broken.settings.json").write_text("{not json", encoding="utf-8")
    entry = [m for m in ms.list_members(d)["members"] if m["name"] == "broken"][0]
    check("unparseable file listed with an error", "error" in entry)

    # Blocks that disagree are flagged.
    q = write_member(d, "qualia", auto_wake=None)
    data = json.loads(q.read_text())
    data["pluginConfigs"]["haiku"]["options"]["eotKind"] = "git"
    data["pluginConfigs"]["haiku@inline"]["options"]["eotKind"] = "file"
    q.write_text(json.dumps(data))
    entry = [m for m in ms.list_members(d)["members"] if m["name"] == "qualia"][0]
    check("disagreeing blocks flagged not in_sync", entry["in_sync"] is False)
    out = ms.update_member(d, "qualia", {"eotKind": "git"})
    check("saving brings the blocks back in sync", out["in_sync"] is True)


def http_tests(d):
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        if os.path.exists(DBFILE + ext):
            os.remove(DBFILE + ext)
    holder = {}

    def run():
        conn = db.connect(DBFILE)
        server.Handler.conn = conn
        server.Handler.port = PORT
        server.Handler.settings_dir = Path(d)
        # The startup sweep deletes any attachment file with no row in THIS
        # db, so it must never point at the real store (see test_server.py).
        server.Handler.store_dir = Path(d) / "attachments"
        httpd = server.SweepingHTTPServer((server.HOST, PORT), server.Handler)
        holder["httpd"] = httpd
        httpd.serve_forever()
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while "httpd" not in holder:
        pass

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        h = dict(headers or {})
        data = b""
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw) if raw else {}

    try:
        # Register a human and an AI directly in the db, as the daemon would.
        original = (Path(d) / "vero.settings.json").read_bytes()
        conn = db.connect(DBFILE)
        secret = db._admin_secret_path(DBFILE).read_text().strip()
        human = db.register_human(conn, "Teddy", secret)
        ai = db.register_ai(conn, "Vero")
        conn.close()
        H = {"X-Haiku-Participant": "Teddy", "X-Haiku-Token": human}
        A = {"X-Haiku-Participant": "Vero", "X-Haiku-Token": ai}

        check("no credentials: 401", call("GET", "/settings")[0] == 401)
        check("AI GET /settings: 403", call("GET", "/settings", headers=A)[0] == 403)
        check("AI PUT /settings: 403", call("PUT", "/settings/vero", {"changes": {"autoWake": True}}, A)[0] == 403)
        check("AI could not change anything", (Path(d) / "vero.settings.json").read_bytes() == original)

        s, body = call("GET", "/settings", headers=H)
        check("human GET /settings: 200 with members", s == 200 and any(m["name"] == "vero" for m in body["members"]))
        check("token not in the HTTP response", FAKE_TOKEN not in json.dumps(body))

        s, body = call("PUT", "/settings/vero", {"changes": {"eotCycleLive": True}}, H)
        check("human PUT: 200 and value applied", s == 200 and body["options"]["eotCycleLive"] is True)
        s, body = call("PUT", "/settings/vero", {"changes": {"participantToken": "x"}}, H)
        check("human PUT of the token: 400", s == 400)
        s, body = call("PUT", "/settings/nobody", {"changes": {"autoWake": True}}, H)
        check("human PUT for an unknown member: 400", s == 400)
    finally:
        holder["httpd"].shutdown()
        t.join(timeout=5)
        for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
            if os.path.exists(DBFILE + ext):
                os.remove(DBFILE + ext)


def main():
    with tempfile.TemporaryDirectory(prefix="haiku-test-settings-") as d:
        unit_tests(d)
    with tempfile.TemporaryDirectory(prefix="haiku-test-settings-") as d:
        write_member(d, "vero")
        http_tests(d)
    print("all member_settings tests passed")


if __name__ == "__main__":
    main()
