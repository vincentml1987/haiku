"""
Functional tests for daemon/server.py. Starts a real daemon on a scratch
db/port, exercises it over HTTP, tears it down. Run directly:
`python test_server.py`.
"""

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse

import db
import server

DBFILE = "test_server.db"
PORT = 8799
ATTACH_TMP = tempfile.mkdtemp(prefix="haiku-test-attach-")


def cleanup_files():
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        p = DBFILE + ext
        if os.path.exists(p):
            os.remove(p)


def check(label, cond):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise AssertionError(label)


class Client:
    """Thin HTTP client so test bodies read as intent, not boilerplate."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None, content_type="application/json", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        if host is not None:
            hdrs["Host"] = host
        data = b""
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, (bytes, bytearray)) else body
            if content_type is not None:
                hdrs["Content-Type"] = content_type
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The server can reject (e.g. a 413 over the body cap) before a
            # large client write finishes; on Windows that surfaces as a
            # socket abort rather than a readable response. Treat it as an
            # emphatic rejection, not a test-harness failure (Vero caught
            # this flaking ~1 in 3 runs).
            return None, {"_connection_aborted": True}
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        return resp.status, parsed

    def auth(self, name, token):
        return {"X-Haiku-Participant": name, "X-Haiku-Token": token}


def main():
    cleanup_files()
    # server.py's real entry point (main()) connects and serves from the
    # SAME thread, by design (see server.py's module docstring: one
    # sqlite3 connection, single-threaded). Do the same here — connecting
    # in the test's main thread but serving in a worker thread would hand
    # the connection to a different thread than the one that created it,
    # which sqlite3 rejects; that mismatch is a test-harness artifact, not
    # something the real server does.
    httpd_holder = {}

    def run():
        conn = db.connect(DBFILE)
        server.Handler.conn = conn
        server.Handler.port = PORT
        # Never the real attachments folder: a private temp dir for this run.
        server.Handler.store_dir = ATTACH_TMP
        httpd = server.HTTPServer((server.HOST, PORT), server.Handler)
        httpd_holder["httpd"] = httpd
        httpd_holder["ready"].set()
        httpd.serve_forever()
        conn.close()  # must close from the same thread that created it

    httpd_holder["ready"] = threading.Event()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    httpd_holder["ready"].wait(timeout=5)
    httpd = httpd_holder["httpd"]
    time.sleep(0.2)

    c = Client(PORT)
    try:
        admin_secret = db._admin_secret_path(DBFILE).read_text().strip()

        # --- Host header enforcement ---
        status, resp = c.request("GET", "/rooms", host="evil.example.com")
        check("wrong Host header rejected", status == 403)

        status, resp = c.request("GET", "/rooms", host="127.0.0.1:" + str(PORT))
        check("correct Host header accepted (reaches the handler, which wants auth)", status == 401)
        status, resp = c.request("GET", "/rooms")
        check("GET /rooms requires auth", status == 401)

        # --- Content-Type enforcement on POST ---
        status, resp = c.request("POST", "/register/ai", body={"name": "NoCT"}, content_type=None)
        check("missing Content-Type rejected", status == 400)

        status, resp = c.request("POST", "/register/ai", body=b"name=NoCT", content_type="text/plain")
        check("wrong Content-Type rejected", status == 400)

        # --- non-object JSON body ---
        status, resp = c.request("POST", "/register/ai", body=["not", "an", "object"])
        check("non-object JSON body rejected with 400 not 500", status == 400)

        # --- oversized body ---
        status, resp = c.request(
            "POST", "/register/ai",
            body=json.dumps({"name": "X", "address": "A" * (server.MAX_BODY_BYTES + 10)}).encode(),
        )
        # Either a clean 413, or the connection aborted mid-send (Windows
        # surfaces the server's early reject that way) — both mean rejected.
        check("oversized body rejected (413, or connection aborted)", status == 413 or resp.get("_connection_aborted"))

        # --- registration ---
        status, resp = c.request("POST", "/register/human", body={"name": "Teddy"},
                                  headers={"X-Haiku-Admin-Secret": "wrong"})
        check("human registration with wrong admin secret rejected", status == 400)

        status, resp = c.request("POST", "/register/human", body={"name": "Teddy"},
                                  headers={"X-Haiku-Admin-Secret": admin_secret})
        check("human registration with correct admin secret succeeds", status == 200)
        teddy_tok = resp["token"]

        status, resp = c.request("POST", "/register/ai", body={"name": "Qualia"})
        check("AI self-registration succeeds", status == 200)
        qualia_tok = resp["token"]

        # --- auth headers required, token never accepted via query/body ---
        status, resp = c.request("POST", "/rooms", body={"name": "testroom"})
        check("room creation without auth headers rejected", status == 401)

        status, resp = c.request("POST", "/rooms", body={"name": "testroom"}, headers=c.auth("Teddy", "wrong-token"))
        check("room creation with bad token rejected", status == 400)

        status, resp = c.request("POST", "/rooms", body={"name": "testroom"}, headers=c.auth("Teddy", teddy_tok))
        check("room creation with valid auth succeeds", status == 200)
        room_id = resp["room_id"]

        # --- closed room join rejected without invite, over HTTP ---
        status, resp = c.request("POST", f"/rooms/{room_id}/join", body={}, headers=c.auth("Qualia", qualia_tok))
        check("uninvited HTTP join to closed room rejected", status == 400)

        status, resp = c.request(
            "POST", f"/rooms/{room_id}/invite", body={"invitee": "Qualia"}, headers=c.auth("Teddy", teddy_tok)
        )
        check("invite over HTTP succeeds", status == 200)

        status, resp = c.request("POST", f"/rooms/{room_id}/join", body={}, headers=c.auth("Qualia", qualia_tok))
        check("invited HTTP join succeeds", status == 200)

        # --- send / read round trip, token only in headers ---
        status, resp = c.request(
            "POST", f"/rooms/{room_id}/send", body={"body": "hello"}, headers=c.auth("Teddy", teddy_tok)
        )
        check("send succeeds", status == 200)

        status, resp = c.request("GET", f"/rooms/{room_id}/events", headers=c.auth("Qualia", qualia_tok))
        check("read succeeds and sees the message", status == 200 and any(
            e["type"] == "message" and e["body"] == "hello" for e in resp["events"]
        ))

        status, resp = c.request("GET", f"/rooms/{room_id}/events?token=" + qualia_tok)
        check("token in query string alone is not accepted as auth", status == 401)

        # --- §7 additions: /me/rooms, /participants, pause, archive ---
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("Teddy", teddy_tok))
        check("my_rooms succeeds and includes the test room", status == 200 and any(
            r["id"] == room_id for r in resp["rooms"]
        ))
        my_room = next(r for r in resp["rooms"] if r["id"] == room_id)
        check("my_rooms exposes last_seq/my_cursor (the UI's field names)",
              "last_seq" in my_room and "my_cursor" in my_room)

        # A participant who has joined but never read anything (no ack,
        # no delivery yet): my_cursor must be a clean 0, not null/missing —
        # the page's divider logic (Number.isInteger check) depends on this.
        status, resp = c.request("POST", "/register/ai", body={"name": "NeverAcked"})
        never_acked_tok = resp["token"]
        status, resp = c.request(
            "POST", f"/rooms/{room_id}/invite", body={"invitee": "NeverAcked"}, headers=c.auth("Teddy", teddy_tok)
        )
        check("invite NeverAcked succeeds", status == 200)
        status, resp = c.request("POST", f"/rooms/{room_id}/join", body={}, headers=c.auth("NeverAcked", never_acked_tok))
        check("NeverAcked join succeeds", status == 200)
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("NeverAcked", never_acked_tok))
        never_room = next(r for r in resp["rooms"] if r["id"] == room_id)
        check("never-acked participant has my_cursor == 0, not null", never_room["my_cursor"] == 0)
        check("never-acked participant's unread equals the full last_seq", never_room["unread"] == never_room["last_seq"])

        status, resp = c.request("GET", "/participants", headers=c.auth("Teddy", teddy_tok))
        names = {p["name"]: p["kind"] for p in resp["participants"]}
        check("participants lists Teddy and Qualia with kinds", names.get("Teddy") == "human" and names.get("Qualia") == "ai")

        status, resp = c.request("GET", "/participants")
        check("participants requires auth", status == 401)

        # --- lobby / scoped participants / pending invites over HTTP ---
        status, resp = c.request("POST", "/register/ai", body={"name": "LobbyCheck"})
        lobby_tok = resp["token"]
        check("AI registration reports the lobby exists, not joined",
              status == 200 and resp.get("lobby", {}).get("name") == "lobby" and resp["lobby"]["joined"] is False)
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("LobbyCheck", lobby_tok))
        check("registering did not join the lobby", status == 200 and resp["rooms"] == [])
        check("/me/rooms carries pending_invites (empty)", resp.get("pending_invites") == [])
        status, resp = c.request("GET", "/participants", headers=c.auth("LobbyCheck", lobby_tok))
        check("an AI sharing no room sees nobody over HTTP", status == 200 and resp["participants"] == [])
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("Teddy", teddy_tok))
        lobby_id = next(r["id"] for r in resp["rooms"] if r["name"] == "lobby")
        status, resp = c.request("POST", f"/rooms/{lobby_id}/archive", body={}, headers=c.auth("Teddy", teddy_tok))
        check("archiving the lobby is rejected over HTTP", status == 400)
        status, resp = c.request("POST", "/rooms", body={"name": "lobby"}, headers=c.auth("Teddy", teddy_tok))
        check("creating a second lobby is rejected over HTTP", status == 400)
        status, resp = c.request("POST", "/rooms", body={"name": "inv-http"}, headers=c.auth("Teddy", teddy_tok))
        inv_id = resp["room_id"]
        c.request("POST", f"/rooms/{inv_id}/invite", body={"invitee": "LobbyCheck"}, headers=c.auth("Teddy", teddy_tok))
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("LobbyCheck", lobby_tok))
        check("invite shows up in /me/rooms pending_invites",
              resp["pending_invites"] == [{"room_id": inv_id, "room_name": "inv-http", "invited_by": "Teddy"}])

        # --- auto-wake kill switch (spec 3a level 3) ---
        check("/me/rooms reports wake_allowed true by default", resp.get("wake_allowed") is True)
        status, resp = c.request("PUT", "/participants/LobbyCheck/wake_allowed", body={"allowed": False},
                                 headers=c.auth("LobbyCheck", lobby_tok))
        check(f"an AI cannot set wake_allowed (403) got {status} {resp}", status == 403)
        status, resp = c.request("PUT", "/participants/LobbyCheck/wake_allowed", body={"allowed": False})
        check("setting wake_allowed requires auth", status == 401)
        status, resp = c.request("PUT", "/participants/LobbyCheck/wake_allowed", body={"allowed": "no"},
                                 headers=c.auth("Teddy", teddy_tok))
        check("wake_allowed must be a real boolean", status == 400)
        status, resp = c.request("PUT", "/participants/Nobody/wake_allowed", body={"allowed": False},
                                 headers=c.auth("Teddy", teddy_tok))
        check("wake_allowed on an unknown participant is 400", status == 400)
        status, resp = c.request("PUT", "/participants/LobbyCheck/wake_allowed", body={"allowed": False},
                                 headers=c.auth("Teddy", teddy_tok))
        check(f"a human can withhold waking got {status} {resp}", status == 200 and resp["wake_allowed"] is False)
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("LobbyCheck", lobby_tok))
        check("the watcher sees wake_allowed false in /me/rooms", resp.get("wake_allowed") is False)
        status, resp = c.request("GET", "/participants", headers=c.auth("Teddy", teddy_tok))
        check("a human's participant list shows wake_allowed",
              any(p["name"] == "LobbyCheck" and p["wake_allowed"] is False for p in resp["participants"]))
        c.request("PUT", "/participants/LobbyCheck/wake_allowed", body={"allowed": True}, headers=c.auth("Teddy", teddy_tok))
        status, resp = c.request("GET", "/me/rooms", headers=c.auth("LobbyCheck", lobby_tok))
        check("a human can restore waking", resp.get("wake_allowed") is True)

        # --- room metadata / roster scoping (Vero's read-only pass, 2026-10-02) ---
        status, resp = c.request("GET", f"/rooms/{room_id}")
        check("GET /rooms/{id} requires auth", status == 401)
        status, resp = c.request("GET", "/rooms", headers=c.auth("Teddy", teddy_tok))
        check("human sees every room in full over HTTP",
              status == 200 and {room_id, lobby_id, inv_id} <= {r["id"] for r in resp["rooms"]}
              and all("created_by" in r for r in resp["rooms"]))
        status, resp = c.request("GET", "/rooms", headers=c.auth("LobbyCheck", lobby_tok))
        listed = {r["name"]: r for r in resp["rooms"]}
        check("non-member AI sees the open lobby (public fields only)",
              "lobby" in listed and set(listed["lobby"]) == {"id", "name", "topic", "mode", "state"})
        check("non-member AI does not see a closed room it has no invite to", "testroom" not in listed)
        check("invitee sees the room it is invited to, public fields only",
              "inv-http" in listed and set(listed["inv-http"]) == {"id", "name", "topic", "mode", "state"})
        status, resp = c.request("GET", f"/rooms/{room_id}", headers=c.auth("LobbyCheck", lobby_tok))
        check("non-member AI gets 403 and no roster for a closed room", status == 403 and "roster" not in resp)
        status, resp = c.request("GET", f"/rooms/{lobby_id}", headers=c.auth("LobbyCheck", lobby_tok))
        check("non-member AI sees no roster for an open room",
              status == 200 and "roster" not in resp and "created_by" not in resp)
        status, resp = c.request("GET", f"/rooms/{inv_id}", headers=c.auth("LobbyCheck", lobby_tok))
        check("pending invitee gets public fields, no roster",
              status == 200 and "roster" not in resp and resp["name"] == "inv-http")
        status, resp = c.request("GET", f"/rooms/{room_id}", headers=c.auth("Qualia", qualia_tok))
        check("a member AI still gets the full room with roster", status == 200 and "roster" in resp)
        status, resp = c.request("GET", f"/rooms/{room_id}", headers=c.auth("Teddy", teddy_tok))
        check("a human still gets the full room with roster", status == 200 and "roster" in resp)
        status, resp = c.request("GET", f"/rooms/{room_id}/events", headers=c.auth("LobbyCheck", lobby_tok))
        check("non-member events read is 403, not 400", status == 403)

        status, resp = c.request("POST", f"/rooms/{room_id}/pause", body={}, headers=c.auth("Qualia", qualia_tok))
        check("AI cannot pause over HTTP", status == 400)

        # An AI token must never be able to pause/resume/archive, whatever
        # state the room is in — a UI bug surfacing these controls to an AI
        # must still hit a hard daemon-side wall (Vero's ask, after her e2e
        # pass deliberately stopped short of exercising human-only flows).
        status, resp = c.request("POST", f"/rooms/{room_id}/resume", body={}, headers=c.auth("Qualia", qualia_tok))
        check("AI cannot resume over HTTP (even on an active, non-paused room)", status == 400)

        status, resp = c.request("POST", f"/rooms/{room_id}/pause", body={}, headers=c.auth("Teddy", teddy_tok))
        check("human pause over HTTP succeeds", status == 200)

        status, resp = c.request("POST", f"/rooms/{room_id}/resume", body={}, headers=c.auth("Teddy", teddy_tok))
        check("resume after HTTP pause succeeds", status == 200)

        status, resp = c.request("POST", f"/rooms/{room_id}/archive", body={}, headers=c.auth("Qualia", qualia_tok))
        check("AI cannot archive over HTTP", status == 400)

        status, resp = c.request("POST", f"/rooms/{room_id}/archive", body={}, headers=c.auth("Teddy", teddy_tok))
        check("human archive over HTTP succeeds", status == 200)

        # --- static UI serving + CSP ---
        status, resp = c.request("GET", "/ui/app.css")
        check("missing UI file responds cleanly (not a crash)", status in (200, 500))

        status, resp = c.request("GET", "/nonexistent-route-for-csp-check")
        csp_present = False  # checked via raw headers below
        conn2 = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        conn2.request("GET", "/nonexistent-route-for-csp-check", headers={"Host": f"127.0.0.1:{PORT}"})
        raw_resp = conn2.getresponse()
        csp_present = raw_resp.getheader("Content-Security-Policy") is not None
        nosniff = raw_resp.getheader("X-Content-Type-Options") == "nosniff"
        raw_resp.read()
        conn2.close()
        check("CSP header present on an API response", csp_present)
        check("nosniff header present on an API response", nosniff)

        # --- 404 ---
        status, resp = c.request("GET", "/nonsense")
        check("unknown route is 404", status == 404)

        # --- attachments (2026-10-04) ---
        status, resp = c.request("POST", "/rooms", body={"name": "attach-room", "mode": "open"}, headers=c.auth("Teddy", teddy_tok))
        aroom = resp["room_id"]
        c.request("POST", f"/rooms/{aroom}/join", body={}, headers=c.auth("Qualia", qualia_tok))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

        def upload(who, tok, name, data, ctype="application/octet-stream", room=None):
            h = dict(c.auth(who, tok))
            h["X-Haiku-Filename"] = urllib.parse.quote(name)
            return c.request("POST", f"/rooms/{room or aroom}/attachments", body=data, headers=h, content_type=ctype)

        status, att = upload("Teddy", teddy_tok, "../../..\\evil dir\\shot.png", png)
        check("a png upload succeeds", status == 200 and att.get("mime") == "image/png" and att.get("size") == len(png))
        check("the display filename drops every path component", att.get("filename") == "shot.png")
        stored = os.listdir(ATTACH_TMP)
        check("the file is stored only as <random id>.<sniffed ext> inside the store dir",
              stored == [f"{att['id']}.png"])
        check("local_path points inside the store dir",
              os.path.dirname(att.get("local_path", "")) == os.path.realpath(ATTACH_TMP))

        for label, name, data, ctype, want in [
            ("html is refused", "page.html", b"<html><script>1</script></html>", "application/octet-stream", 400),
            ("svg is refused", "pic.svg", b"<svg onload=1></svg>", "application/octet-stream", 400),
            ("a .png whose bytes are not png is refused", "fake.png", b"GIF89a....", "application/octet-stream", 400),
            ("an exe renamed .txt is refused (NUL bytes)", "tool.txt", b"MZ\x90\x00\x03\x00", "application/octet-stream", 400),
            ("invalid json is refused", "data.json", b"{not json", "application/octet-stream", 400),
            ("a form content type is refused", "a.txt", b"hello", "application/x-www-form-urlencoded", 400),
            ("no extension is refused", "README", b"hello", "application/octet-stream", 400),
        ]:
            status, _ = upload("Teddy", teddy_tok, name, data, ctype)
            check(label, status == want)
        check("refused uploads leave nothing on disk", len(os.listdir(ATTACH_TMP)) == 1)

        status, _ = c.request("POST", f"/rooms/{aroom}/attachments", body=png,
                              headers={**c.auth("Teddy", teddy_tok)}, content_type="application/octet-stream")
        check("missing X-Haiku-Filename is refused", status == 400)
        status, _ = upload("Teddy", "wrong-token", "a.png", png)
        check("a bad token cannot upload", status == 400)
        status, resp = c.request("POST", "/register/ai", body={"name": "Outsider"})
        out_tok = resp["token"]
        status, _ = upload("Outsider", out_tok, "a.png", png)
        check("a non-member cannot upload", status == 403)

        status, txt = upload("Qualia", qualia_tok, "notes.md", b"# hi\n")
        check("an AI can upload a markdown file", status == 200 and txt.get("mime", "").startswith("text/markdown"))

        # Unbound: only the uploader can fetch it.
        status, _ = c.request("GET", f"/rooms/{aroom}/attachments/{txt['id']}", headers=c.auth("Teddy", teddy_tok))
        check("someone else's unsent upload cannot be fetched", status == 400)

        status, _ = c.request("POST", f"/rooms/{aroom}/send", body={"body": "", "attachment_ids": [txt["id"], att["id"]]},
                              headers=c.auth("Qualia", qualia_tok))
        check("you cannot send someone else's upload", status == 400)
        status, resp = c.request("POST", f"/rooms/{aroom}/send", body={"body": "see screenshot", "attachment_ids": [att["id"]]},
                                 headers=c.auth("Teddy", teddy_tok))
        check("a message with an attachment sends", status == 200)
        sent_seq = resp["seq"]
        status, _ = c.request("POST", f"/rooms/{aroom}/send", body={"body": "again", "attachment_ids": [att["id"]]},
                              headers=c.auth("Teddy", teddy_tok))
        check("an attachment can only be sent once", status == 400)

        status, resp = c.request("GET", f"/rooms/{aroom}/events?since=0", headers=c.auth("Qualia", qualia_tok))
        msg = [e for e in resp["events"] if e["seq"] == sent_seq][0]
        check("read_events carries the attachment with display name, type, size and local path",
              msg.get("attachments") and msg["attachments"][0]["filename"] == "shot.png"
              and msg["attachments"][0]["local_path"].endswith(f"{att['id']}.png"))

        def raw_get(who, tok, aid):
            conn3 = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
            conn3.request("GET", f"/rooms/{aroom}/attachments/{aid}", headers={**c.auth(who, tok), "Host": f"127.0.0.1:{PORT}"})
            r = conn3.getresponse()
            body = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            conn3.close()
            return r.status, hdrs, body

        status, hdrs, body = raw_get("Qualia", qualia_tok, att["id"])
        check("a member can fetch a sent attachment, bytes intact", status == 200 and body == png)
        check("served with its stored type, nosniff and a sandboxing CSP",
              hdrs.get("content-type") == "image/png" and hdrs.get("x-content-type-options") == "nosniff"
              and "sandbox" in hdrs.get("content-security-policy", ""))
        check("images are inline", hdrs.get("content-disposition", "").startswith("inline"))

        # Tessera's polyglot: a real PNG header followed by HTML/script is
        # accepted (the sniff is header-only), so what keeps it inert is the
        # serving trio. That trio is a tested REQUIREMENT, not a nicety.
        poly = b"\x89PNG\r\n\x1a\n<html><script>window.__pwned=1</script></html>"
        status, patt = upload("Teddy", teddy_tok, "poly.png", poly)
        c.request("POST", f"/rooms/{aroom}/send", body={"body": "poly", "attachment_ids": [patt["id"]]},
                  headers=c.auth("Teddy", teddy_tok))
        status, phdrs, _ = raw_get("Teddy", teddy_tok, patt["id"])
        check("a PNG/HTML polyglot is served as image/png, never text/html",
              status == 200 and phdrs.get("content-type") == "image/png")
        check("...with nosniff, so no browser re-guesses it as HTML", phdrs.get("x-content-type-options") == "nosniff")
        check("...and a sandboxing CSP with default-src 'none', so even rendered HTML could run nothing",
              "sandbox" in phdrs.get("content-security-policy", "")
              and "default-src 'none'" in phdrs.get("content-security-policy", ""))

        # Paused room: an AI's upload is refused (before its body is read).
        c.request("POST", f"/rooms/{aroom}/pause", body={}, headers=c.auth("Teddy", teddy_tok))
        status, _ = upload("Qualia", qualia_tok, "p.png", png)
        check("an AI cannot upload into a paused room over HTTP", status == 400)
        c.request("POST", f"/rooms/{aroom}/resume", body={}, headers=c.auth("Teddy", teddy_tok))

        # Slow drip: the overall read deadline cuts it off (shortened for the test).
        saved_deadline = server.ATTACH_READ_DEADLINE_S
        server.ATTACH_READ_DEADLINE_S = 2
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
            s.sendall((f"POST /rooms/{aroom}/attachments HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
                       f"X-Haiku-Participant: Teddy\r\nX-Haiku-Token: {teddy_tok}\r\nX-Haiku-Filename: slow.txt\r\n"
                       f"Content-Type: application/octet-stream\r\nContent-Length: 1000\r\n\r\n").encode())
            t0 = time.monotonic()
            s.settimeout(0.5)  # each recv attempt doubles as the drip interval
            resp = b""
            for _ in range(16):
                try:
                    s.sendall(b"a")
                except OSError:
                    break  # the server closed after answering
                try:
                    resp = s.recv(4096)
                    if resp:
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
            s.close()
            check("a slow-drip upload is cut off by the overall deadline (408), not held open",
                  b" 408 " in resp.split(b"\r\n", 1)[0] and time.monotonic() - t0 < 8)
        finally:
            server.ATTACH_READ_DEADLINE_S = saved_deadline

        status, hdrs, _ = raw_get("Qualia", qualia_tok, txt["id"])
        check("non-images download as attachment", status == 200 and hdrs.get("content-disposition", "").startswith("attachment"))
        status, _, _ = raw_get("Outsider", out_tok, att["id"])
        check("a non-member cannot fetch", status == 403)
        c.request("POST", f"/rooms/{aroom}/leave", body={}, headers=c.auth("Qualia", qualia_tok))
        status, _, _ = raw_get("Qualia", qualia_tok, att["id"])
        check("a member who left can no longer fetch (checked every request)", status == 403)
        status, _ = c.request("GET", f"/rooms/{aroom}/attachments/..%2F..%2Fhaiku.db", headers=c.auth("Teddy", teddy_tok))
        check("a non-hex attachment id never reaches the filesystem", status == 404)

        print("\nALL CHECKS PASSED")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        cleanup_files()
        shutil.rmtree(ATTACH_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
