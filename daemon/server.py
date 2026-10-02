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

Hardening against a hostile page in Teddy's own browser, not just a
remote network attacker (127.0.0.1 binding alone doesn't stop that —
localhost HTTP servers are a known cross-site target): every request's
Host header must say 127.0.0.1/localhost on this port, every POST must
declare Content-Type: application/json (a plain cross-origin form POST
can't set that without triggering a CORS preflight the server never
answers), and no CORS headers are ever sent.

Identity (participant + token, or the admin secret) travels in request
HEADERS, never in the JSON body or a query string — a query string ends
up in shell history, proxy logs, and crash traces in a way a header is
less likely to.

See the README's "Threat model" section for what this auth layer does
and doesn't protect against.
"""

import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import db

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DB_PATH = str(Path(__file__).parent / "haiku.db")
MAX_BODY_BYTES = 1_000_000
SOCKET_TIMEOUT_S = 10

ROOM_ID = r"(?P<room_id>[^/]+)"
ROUTES = []  # (method, compiled_path_re, handler_name)


class ClientError(Exception):
    """A 4xx the caller should see as a clear message, not a 500."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


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


def _auth_headers(headers):
    """(participant, token) from X-Haiku-Participant / X-Haiku-Token."""
    name = headers.get("X-Haiku-Participant")
    token = headers.get("X-Haiku-Token")
    if not name or not token:
        raise ClientError(401, "missing X-Haiku-Participant / X-Haiku-Token headers")
    return name, token


def _admin_header(headers):
    secret = headers.get("X-Haiku-Admin-Secret")
    if not secret:
        raise ClientError(401, "missing X-Haiku-Admin-Secret header")
    return secret


@route("POST", r"/register/ai")
def h_register_ai(conn, params, body, headers):
    token = db.register_ai(conn, body["name"], address=body.get("address"))
    return {"token": token}


@route("POST", r"/register/human")
def h_register_human(conn, params, body, headers):
    token = db.register_human(conn, body["name"], _admin_header(headers), address=body.get("address"))
    return {"token": token}


@route("POST", r"/rotate_token")
def h_rotate_token(conn, params, body, headers):
    name = body["name"]
    admin_secret = headers.get("X-Haiku-Admin-Secret")
    if admin_secret:
        token = db.rotate_token(conn, name, admin_secret, credential_is_admin_secret=True)
    else:
        _, current_token = _auth_headers(headers)
        token = db.rotate_token(conn, name, current_token)
    return {"token": token}


@route("POST", r"/rooms")
def h_create_room(conn, params, body, headers):
    creator, token = _auth_headers(headers)
    room_id = db.create_room(
        conn, body["name"], creator, token,
        topic=body.get("topic"), mode=body.get("mode", "closed"),
        hop_limit=body.get("hop_limit", 6),
    )
    return {"room_id": room_id}


@route("GET", r"/rooms")
def h_list_rooms(conn, params, body, headers):
    state = params.get("state", [None])[0]
    return {"rooms": db.list_rooms(conn, state=state)}


@route("GET", f"/rooms/{ROOM_ID}")
def h_get_room(conn, params, body, headers, room_id):
    room = db.get_room(conn, room_id)
    room["roster"] = db.room_roster(conn, room_id)
    return room


@route("POST", f"/rooms/{ROOM_ID}/invite")
def h_invite(conn, params, body, headers, room_id):
    inviter, token = _auth_headers(headers)
    db.invite(conn, room_id, inviter, token, body["invitee"])
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/join")
def h_join(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)
    db.join_room(conn, room_id, participant, token, catch_up=body.get("catch_up"))
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/leave")
def h_leave(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)
    db.leave_room(conn, room_id, participant, token)
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/send")
def h_send(conn, params, body, headers, room_id):
    author, token = _auth_headers(headers)
    return db.send_message(conn, room_id, author, token, body["body"], addressed_to=body.get("addressed_to"))


@route("POST", f"/rooms/{ROOM_ID}/pass")
def h_pass(conn, params, body, headers, room_id):
    author, token = _auth_headers(headers)
    return {"seq": db.send_pass(conn, room_id, author, token)}


@route("POST", f"/rooms/{ROOM_ID}/resume")
def h_resume(conn, params, body, headers, room_id):
    resumer, token = _auth_headers(headers)
    db.resume_room(conn, room_id, resumer, token, granted_hops=body.get("granted_hops"))
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/topic")
def h_topic(conn, params, body, headers, room_id):
    author, token = _auth_headers(headers)
    return {"seq": db.set_topic(conn, room_id, author, token, body["topic"])}


@route("GET", f"/rooms/{ROOM_ID}/events")
def h_events(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)

    def _int(name):
        v = params.get(name, [None])[0]
        return int(v) if v is not None else None

    return db.read_events(
        conn, room_id, participant, token,
        since=_int("since"), limit=_int("limit"),
        advance=_bool(params.get("advance", [None])[0], default=True),
        exclude_self=_bool(params.get("exclude_self", [None])[0], default=False),
    )


@route("POST", f"/rooms/{ROOM_ID}/ack")
def h_ack(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)
    db.ack(conn, room_id, participant, token, body["through_seq"])
    return {"ok": True}


def _host_ok(host_header: str, port: int) -> bool:
    host = (host_header or "").split(":")[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    conn = None  # set by main() before serving
    port = DEFAULT_PORT
    timeout = SOCKET_TIMEOUT_S  # socketserver applies this as the request socket timeout

    def _respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self):
        if self.command != "POST":
            return {}
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise ClientError(400, "POST requires Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ClientError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ClientError(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        try:
            raw = self.rfile.read(length) if length else b"{}"
        except (socket.timeout, TimeoutError):
            raise ClientError(408, "request body timed out")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise ClientError(400, "invalid JSON body")
        if not isinstance(parsed, dict):
            raise ClientError(400, "JSON body must be an object")
        return parsed

    def _dispatch(self, method):
        if not _host_ok(self.headers.get("Host", ""), self.port):
            return self._respond(403, {"error": "unrecognized Host header"})

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        try:
            body = self._read_json_body()
        except ClientError as e:
            return self._respond(e.status, {"error": e.message})

        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            m = pattern.match(parsed.path)
            if m:
                try:
                    result = handler(self.conn, params, body, self.headers, **m.groupdict())
                    return self._respond(200, result)
                except ClientError as e:
                    return self._respond(e.status, {"error": e.message})
                except db.HaikuError as e:
                    return self._respond(400, {"error": str(e)})
                except KeyError as e:
                    return self._respond(400, {"error": f"missing required field: {e}"})
                except Exception as e:  # noqa: BLE001 — last resort, never leak internals to a client
                    print(f"[haiku daemon] internal error on {method} {parsed.path}: {e!r}", file=sys.stderr)
                    return self._respond(500, {"error": "internal error"})

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
    Handler.port = port

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
