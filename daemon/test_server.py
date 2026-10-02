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
        status, resp = c.request("POST", "/rooms", body={"name": "lobby"})
        check("room creation without auth headers rejected", status == 401)

        status, resp = c.request("POST", "/rooms", body={"name": "lobby"}, headers=c.auth("Teddy", "wrong-token"))
        check("room creation with bad token rejected", status == 400)

        status, resp = c.request("POST", "/rooms", body={"name": "lobby"}, headers=c.auth("Teddy", teddy_tok))
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
