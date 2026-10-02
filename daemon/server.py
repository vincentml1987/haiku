"""
HAIKU daemon HTTP layer. Thin translation from JSON requests to db.py
calls — no room/auth logic lives here, it all lives in db.py.

Binds 127.0.0.1 ONLY, hardcoded, not a flag. This is a single-user local
tool (Teddy + his own AI collaborators on his own machine); there is no
reason for it to ever be reachable off-box, and no option here to make it
so by accident.

Single-threaded by design (plain HTTPServer, not ThreadingHTTPServer) —
one sqlite3 connection, no cross-thread sqlite concerns, and traffic here
is inherently low-volume: sessions call in at most once per turn, per the
room spec's own "away is normal" premise. No reason to add concurrency
this doesn't need.
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import db

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DB_PATH = str(Path(__file__).parent / "haiku.db")

ROOM_ID = r"(?P<room_id>[^/]+)"
ROUTES = []  # (method, compiled_path_re, handler_name)


def route(method, pattern):
    compiled = re.compile(f"^{pattern}$")

    def register(fn):
        ROUTES.append((method, compiled, fn))
        return fn

    return register


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes")


@route("POST", r"/register/ai")
def h_register_ai(conn, params, body):
    token = db.register_ai(conn, body["name"], address=body.get("address"))
    return {"token": token}


@route("POST", r"/register/human")
def h_register_human(conn, params, body):
    token = db.register_human(conn, body["name"], body["admin_secret"], address=body.get("address"))
    return {"token": token}


@route("POST", r"/rotate_token")
def h_rotate_token(conn, params, body):
    token = db.rotate_token(
        conn, body["name"], body["credential"],
        credential_is_admin_secret=_bool(body.get("credential_is_admin_secret")),
    )
    return {"token": token}


@route("POST", r"/rooms")
def h_create_room(conn, params, body):
    room_id = db.create_room(
        conn, body["name"], body["created_by"], body["token"],
        topic=body.get("topic"), mode=body.get("mode", "closed"),
        hop_limit=body.get("hop_limit", 6),
    )
    return {"room_id": room_id}


@route("GET", r"/rooms")
def h_list_rooms(conn, params, body):
    state = params.get("state", [None])[0]
    return {"rooms": db.list_rooms(conn, state=state)}


@route("GET", f"/rooms/{ROOM_ID}")
def h_get_room(conn, params, body, room_id):
    room = db.get_room(conn, room_id)
    room["roster"] = db.room_roster(conn, room_id)
    return room


@route("POST", f"/rooms/{ROOM_ID}/join")
def h_join(conn, params, body, room_id):
    db.join_room(conn, room_id, body["participant"], body["token"], catch_up=body.get("catch_up"))
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/leave")
def h_leave(conn, params, body, room_id):
    db.leave_room(conn, room_id, body["participant"], body["token"])
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/send")
def h_send(conn, params, body, room_id):
    return db.send_message(
        conn, room_id, body["author"], body["token"], body["body"],
        addressed_to=body.get("addressed_to"),
    )


@route("POST", f"/rooms/{ROOM_ID}/pass")
def h_pass(conn, params, body, room_id):
    seq = db.send_pass(conn, room_id, body["author"], body["token"])
    return {"seq": seq}


@route("POST", f"/rooms/{ROOM_ID}/resume")
def h_resume(conn, params, body, room_id):
    db.resume_room(conn, room_id, body["resumed_by"], body["token"], granted_hops=body.get("granted_hops"))
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/topic")
def h_topic(conn, params, body, room_id):
    seq = db.set_topic(conn, room_id, body["author"], body["token"], body["topic"])
    return {"seq": seq}


@route("GET", f"/rooms/{ROOM_ID}/events")
def h_events(conn, params, body, room_id):
    def _int(name):
        v = params.get(name, [None])[0]
        return int(v) if v is not None else None

    events = db.read_events(
        conn, room_id, params["participant"][0], params["token"][0],
        since=_int("since"), limit=_int("limit"),
        advance=_bool(params.get("advance", [None])[0], default=True),
        exclude_self=_bool(params.get("exclude_self", [None])[0], default=False),
    )
    return {"events": events}


@route("POST", f"/rooms/{ROOM_ID}/ack")
def h_ack(conn, params, body, room_id):
    db.ack(conn, room_id, body["participant"], body["token"], body["through_seq"])
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    conn = None  # set by main() before serving

    def _respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return self._respond(400, {"error": "invalid JSON body"})

        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            m = pattern.match(parsed.path)
            if m:
                try:
                    result = handler(self.conn, params, body, **m.groupdict())
                    return self._respond(200, result)
                except db.HaikuError as e:
                    return self._respond(400, {"error": str(e)})
                except KeyError as e:
                    return self._respond(400, {"error": f"missing required field: {e}"})
                except Exception as e:  # noqa: BLE001 — last resort, never leak a raw traceback to a client
                    return self._respond(500, {"error": f"internal error: {e}"})

        self._respond(404, {"error": "no such route"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass  # quiet by default; rooms already have their own event log


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    conn = db.connect(db_path)
    Handler.conn = conn

    server = HTTPServer((HOST, port), Handler)
    print(f"HAIKU daemon listening on http://{HOST}:{port} (db: {db_path})")
    print(f"Admin secret: {db._admin_secret_path(db_path)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
