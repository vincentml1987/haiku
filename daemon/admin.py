"""
daemon/admin.py — local-only admin commands, run by Teddy himself as his
own OS user. Never imported by server.py or db.py; the admin secret only
ever touches this process and the daemon's own.

ui-spec.md §2: `login` is how Teddy's human token gets into the browser
without the admin secret ever reaching it. It reads <db>.admin_secret,
registers (or, if already registered, rotates) his human token, and opens
the UI with the token in the URL FRAGMENT — which the browser never sends
to any server and the daemon's own access pattern never logs, unlike a
query string or path segment.
"""

import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

import db

DEFAULT_DB_PATH = str(Path(__file__).parent / "haiku.db")
DEFAULT_BASE_URL = "http://127.0.0.1:8787"


def login(db_path: str, base_url: str, name: str) -> str:
    conn = db.connect(db_path)
    try:
        admin_secret = db._admin_secret_path(db_path).read_text().strip()
        try:
            token = db.register_human(conn, name, admin_secret)
        except db.HaikuError:
            # Most likely "already registered" (a prior login) — recover
            # via the admin secret rather than fail a second run.
            token = db.rotate_token(conn, name, admin_secret, credential_is_admin_secret=True)
    finally:
        conn.close()

    url = f"{base_url}/ui#token={quote(token)}&name={quote(name)}"
    return url


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "login":
        print("usage: python admin.py login [name] [db_path] [base_url]", file=sys.stderr)
        sys.exit(1)

    name = sys.argv[2] if len(sys.argv) > 2 else "Teddy"
    db_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_DB_PATH
    base_url = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_BASE_URL

    url = login(db_path, base_url, name)
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass  # the printed URL is enough; opening a browser is a convenience only


if __name__ == "__main__":
    main()
