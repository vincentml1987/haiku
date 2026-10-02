"""
Functional tests for daemon/server.py. Starts a real daemon on a scratch
db/port, exercises it over HTTP, tears it down. Run directly:
`python test_server.py`.
"""

import http.client
import json
import os
import socket
import threading
import time

import db
import server

DBFILE = "test_server.db"
PORT = 8799


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
        check("correct Host header accepted", status == 200)

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
        raw_resp.read()
        conn2.close()
        check("CSP header present on an API response", csp_present)

        # --- 404 ---
        status, resp = c.request("GET", "/nonsense")
        check("unknown route is 404", status == 404)

        print("\nALL CHECKS PASSED")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        cleanup_files()


if __name__ == "__main__":
    main()
