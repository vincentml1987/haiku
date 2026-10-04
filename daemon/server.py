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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

import db

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DB_PATH = str(Path(__file__).parent / "haiku.db")
MAX_BODY_BYTES = 1_000_000
SOCKET_TIMEOUT_S = 10

ROOM_ID = r"(?P<room_id>[^/]+)"
ROUTES = []  # (method, compiled_path_re, handler_name)

# Attachments (2026-10-04) are binary in both directions, so they bypass
# the JSON body reader and JSON responder; see Handler._attachment_*.
ATTACH_READ_DEADLINE_S = 60
SWEEP_INTERVAL_S = 3600
ATTACH_UPLOAD_RE = re.compile(r"^/rooms/(?P<room_id>[^/]+)/attachments$")
ATTACH_GET_RE = re.compile(r"^/rooms/(?P<room_id>[^/]+)/attachments/(?P<att_id>[0-9a-f]{32})$")


def default_store_dir(db_path: str) -> Path:
    """Attachments live next to the db, in a folder named after it, outside
    any path a client controls. Gitignored (daemon/attachments*)."""
    p = Path(db_path)
    return p.parent / ("attachments" if p.name == "haiku.db" else f"{p.stem}.attachments")


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
    resp = {"token": token}
    lobby = db.lobby_info(conn)
    if lobby is not None:
        # Informational only: registration never joins anyone (spec: no auto-join).
        resp["lobby"] = lobby
    return resp


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
    participant, token = _auth_headers(headers)
    db.authenticate(conn, participant, token)
    state = params.get("state", [None])[0]
    return {"rooms": db.list_rooms(conn, state=state, caller=participant)}


@route("GET", f"/rooms/{ROOM_ID}")
def h_get_room(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)
    db.authenticate(conn, participant, token)
    room = db.get_room(conn, room_id, caller=participant)
    if db.can_see_roster(conn, room_id, participant):
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
    return db.send_message(conn, room_id, author, token, body["body"], addressed_to=body.get("addressed_to"),
                           attachment_ids=body.get("attachment_ids"))


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
        store_dir=Handler.store_dir,
    )


@route("POST", f"/rooms/{ROOM_ID}/ack")
def h_ack(conn, params, body, headers, room_id):
    participant, token = _auth_headers(headers)
    db.ack(conn, room_id, participant, token, body["through_seq"])
    return {"ok": True}


@route("GET", r"/me/rooms")
def h_my_rooms(conn, params, body, headers):
    participant, token = _auth_headers(headers)
    rooms = db.list_my_rooms(conn, participant, token)
    return {
        "rooms": rooms,
        "pending_invites": db.list_pending_invites(conn, participant),
        "wake_allowed": db.get_wake_allowed(conn, participant),
    }


@route("GET", r"/participants")
def h_participants(conn, params, body, headers):
    participant, token = _auth_headers(headers)
    db.authenticate(conn, participant, token)
    return {"participants": db.list_participants(conn, participant)}


@route("PUT", r"/participants/(?P<name>[^/]+)/wake_allowed")
def h_set_wake_allowed(conn, params, body, headers, name):
    caller, token = _auth_headers(headers)
    if db.authenticate(conn, caller, token) != "human":
        raise db.Forbidden("only a human may change wake_allowed")
    if not isinstance(body.get("allowed"), bool):
        raise ClientError(400, "allowed must be true or false")
    db.set_wake_allowed(conn, caller, token, unquote(name), body["allowed"])
    return {"ok": True, "name": unquote(name), "wake_allowed": body["allowed"]}


@route("POST", f"/rooms/{ROOM_ID}/pause")
def h_pause(conn, params, body, headers, room_id):
    pauser, token = _auth_headers(headers)
    db.pause_room(conn, room_id, pauser, token, reason=body.get("reason"))
    return {"ok": True}


@route("POST", f"/rooms/{ROOM_ID}/archive")
def h_archive(conn, params, body, headers, room_id):
    archiver, token = _auth_headers(headers)
    new_name = db.archive_room(conn, room_id, archiver, token)
    return {"ok": True, "name": new_name}


@route("PUT", f"/rooms/{ROOM_ID}/mute")
def h_mute(conn, params, body, headers, room_id):
    """The caller mutes/unmutes a room for itself only."""
    participant, token = _auth_headers(headers)
    if not isinstance(body.get("muted"), bool):
        raise ClientError(400, "muted must be true or false")
    db.set_room_muted(conn, room_id, participant, token, body["muted"])
    return {"ok": True, "muted": body["muted"]}


@route("PUT", f"/rooms/{ROOM_ID}/wake_allowed/(?P<name>[^/]+)")
def h_set_room_wake_allowed(conn, params, body, headers, room_id, name):
    caller, token = _auth_headers(headers)
    if not isinstance(body.get("allowed"), bool):
        raise ClientError(400, "allowed must be true or false")
    db.set_room_wake_allowed(conn, caller, token, room_id, unquote(name), body["allowed"])
    return {"ok": True, "name": unquote(name), "room_wake_allowed": body["allowed"]}


def _host_ok(host_header: str, port: int) -> bool:
    host = (host_header or "").split(":")[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


UI_DIR = Path(__file__).parent / "ui"

# ui-spec.md §1: a FIXED allowlist, never a path-joined directory — no
# traversal surface. Add a new UI file here deliberately, never by pattern.
STATIC_FILES = {
    "/ui": (UI_DIR / "index.html", "text/html; charset=utf-8"),
    "/ui/": (UI_DIR / "index.html", "text/html; charset=utf-8"),
    "/ui/app.js": (UI_DIR / "app.js", "application/javascript; charset=utf-8"),
    "/ui/app.css": (UI_DIR / "app.css", "text/css; charset=utf-8"),
}

# ui-spec.md §1: no inline script/style, no external fetches, no CDN, no
# web fonts, no framing. Sent on every UI (and, harmlessly, API) response.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class Handler(BaseHTTPRequestHandler):
    conn = None  # set by main() before serving
    port = DEFAULT_PORT
    store_dir = default_store_dir(DEFAULT_DB_PATH)  # main() sets it from the db path
    timeout = SOCKET_TIMEOUT_S  # socketserver applies this as the request socket timeout

    def _respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        # API responses too, not just static UI files (Tessera's review,
        # 2026-10-04): a JSON body carrying participant text must never be
        # content-sniffed into something a browser would render or run.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _respond_static(self, path):
        entry = STATIC_FILES.get(path)
        if entry is None:
            return False
        file_path, content_type = entry
        try:
            data = file_path.read_bytes()
        except OSError:
            self._respond(500, {"error": "UI file missing"})
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def _read_json_body(self):
        if self.command not in ("POST", "PUT"):
            return {}
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise ClientError(400, f"{self.command} requires Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ClientError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ClientError(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        self._body_consumed = True  # from here on, never drain: the read has begun
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

        if method == "GET" and self._respond_static(parsed.path):
            return

        m = ATTACH_UPLOAD_RE.match(parsed.path) if method == "POST" else None
        if m:
            return self._guarded(self._attachment_upload, m.group("room_id"))
        m = ATTACH_GET_RE.match(parsed.path) if method == "GET" else None
        if m:
            return self._guarded(self._attachment_get, m.group("room_id"), m.group("att_id"))

        self._body_consumed = False
        try:
            body = self._read_json_body()
        except ClientError as e:
            if not self._body_consumed:
                self._drain_small_body()
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
                except db.Forbidden as e:
                    return self._respond(403, {"error": str(e)})
                except db.HaikuError as e:
                    return self._respond(400, {"error": str(e)})
                except KeyError as e:
                    return self._respond(400, {"error": f"missing required field: {e}"})
                except Exception as e:  # noqa: BLE001 — last resort, never leak internals to a client
                    print(f"[haiku daemon] internal error on {method} {parsed.path}: {e!r}", file=sys.stderr)
                    return self._respond(500, {"error": "internal error"})

        self._respond(404, {"error": "no such route"})

    DRAIN_MAX = 64 * 1024

    def _drain_small_body(self):
        """Before answering an early rejection, read and discard a SMALL
        unread body. Closing a socket with unread data makes Windows send a
        reset that can destroy the reply before the client reads it (seen as
        an intermittent test_server failure on 2026-10-04). Large bodies are
        not drained: refusing to spend time on them is the point."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= self.DRAIN_MAX:
            try:
                self.rfile.read(n)
            except (OSError, ValueError):
                pass

    def _guarded(self, fn, *args):
        """Same error mapping as the JSON routes, for the binary ones. An
        upload refused before its body was read drains a small body first
        (see _drain_small_body); _attachment_upload marks when it has read."""
        self._body_consumed = False
        try:
            return fn(*args)
        except (ClientError, db.HaikuError) as e:
            if not self._body_consumed:
                self._drain_small_body()
            if isinstance(e, ClientError):
                return self._respond(e.status, {"error": e.message})
            return self._respond(403 if isinstance(e, db.Forbidden) else 400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print(f"[haiku daemon] internal error on attachment {self.path}: {e!r}", file=sys.stderr)
            return self._respond(500, {"error": "internal error"})

    def _attachment_upload(self, room_id):
        """POST raw bytes. Content-Type must be application/octet-stream
        (never a form type, so a cross-site form can't post here), the
        display filename travels URL-encoded in X-Haiku-Filename, and the
        usual identity headers are required."""
        participant, token = _auth_headers(self.headers)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/octet-stream":
            raise ClientError(400, "upload requires Content-Type: application/octet-stream")
        filename = unquote(self.headers.get("X-Haiku-Filename") or "")
        if not filename:
            raise ClientError(400, "missing X-Haiku-Filename header")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ClientError(411, "Content-Length required")
        if length > db.MAX_ATTACHMENT_BYTES:
            raise ClientError(413, f"file exceeds {db.MAX_ATTACHMENT_BYTES} bytes")
        # Everything that doesn't need the bytes is checked BEFORE reading
        # them (auth, membership, paused room, quota), so a refused upload
        # never ties up the single-threaded daemon for a 20 MB read.
        db.upload_precheck(self.conn, room_id, participant, token, length)
        # An overall deadline, not just the per-recv socket timeout (Tessera's
        # review: one byte every 9 s would otherwise hold the daemon for ages).
        deadline = time.monotonic() + ATTACH_READ_DEADLINE_S
        chunks, got = [], 0
        self._body_consumed = True  # from here on, never drain: the read has begun
        try:
            while got < length:
                if time.monotonic() > deadline:
                    raise ClientError(408, f"upload took longer than {ATTACH_READ_DEADLINE_S} s")
                chunk = self.rfile.read1(min(65536, length - got))
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
        except (socket.timeout, TimeoutError):
            raise ClientError(408, "upload timed out")
        data = b"".join(chunks)
        self._body_consumed = True
        if len(data) != length:
            raise ClientError(400, "upload was cut short")
        att = db.upload_attachment(self.conn, self.store_dir, room_id, participant, token, filename, data)
        return self._respond(200, att)

    def _attachment_get(self, room_id, att_id):
        participant, token = _auth_headers(self.headers)
        att, path = db.get_attachment(self.conn, self.store_dir, room_id, att_id, participant, token)
        data = path.read_bytes()
        is_image = att["mime"].startswith("image/")
        disp = "inline" if is_image else "attachment"
        self.send_response(200)
        self.send_header("Content-Type", att["mime"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{quote(att['filename'], safe='')}")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Even an image response can't run anything on the UI's origin.
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def log_message(self, fmt, *args):
        pass  # quiet by default; rooms already have their own event log


class SweepingHTTPServer(HTTPServer):
    """serve_forever calls service_actions() between requests, on the one
    serving thread, so the hourly attachment sweep shares the daemon's
    single sqlite connection safely (no second thread)."""

    # -inf, not 0.0: time.monotonic() counts from boot, so within the first
    # hour after a reboot "now - 0 >= interval" was false and the startup
    # sweep waited an hour (Tessera's review).
    last_sweep = float("-inf")

    def service_actions(self):
        now = time.monotonic()
        if now - self.last_sweep >= SWEEP_INTERVAL_S:
            self.last_sweep = now
            try:
                r = db.sweep_attachments(Handler.conn, Handler.store_dir)
                if r["expired"] or r["orphans"]:
                    print(f"[haiku daemon] attachment sweep: {r}", flush=True)
            except Exception as e:  # noqa: BLE001 — a failed sweep must never stop serving
                print(f"[haiku daemon] attachment sweep failed: {e!r}", file=sys.stderr, flush=True)


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    conn = db.connect(db_path)
    Handler.conn = conn
    Handler.port = port
    Handler.store_dir = default_store_dir(db_path)

    server = SweepingHTTPServer((HOST, port), Handler)  # first sweep runs at startup
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
