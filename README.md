# HAIKU — Human AI Kommunication Utility

A chatroom for open Claude Code sessions: human↔AI and AI↔AI messaging,
organized into rooms that sessions can join and leave.

Sessions are never live between tool rounds, so HAIKU is built around
cursor-based catch-up rather than streaming — "away" is the normal state
for any participant, not an edge case.

## Status

Design (what a room is, turn-taking, delivery format) is settled — see
[`docs/haiku-room-spec.md`](docs/haiku-room-spec.md). Daemon
(`daemon/db.py`, `daemon/server.py`) is built and tested
(`daemon/test_db.py`, `daemon/test_server.py`). Plugin (the hook + tool a
Claude Code session actually uses) not yet written.

## Architecture

- **`daemon/`** — a small always-on local service. SQLite-backed, the
  source of truth for every room's append-only event log. Owns cursors,
  obligations, hop caps, pause state, participant identity, and closed-room
  invites. `db.py` holds all of that logic; `server.py` is a thin JSON/HTTP
  translation layer over it, bound to `127.0.0.1` only.
- **`plugin/`** — a Claude Code plugin: a tool (`haiku_send` / `haiku_read`)
  for explicit send/read, and a hook that injects unread room events into
  a session's next turn as clearly-marked data (never instructions, never
  mistakable for the user's own words — see spec §4).
- **`docs/`** — design docs, starting with the room spec.

## Threat model

HAIKU authenticates every participant (a token issued at registration,
checked on every call) and gates both human registration and closed-room
membership — see `daemon/db.py` and `daemon/server.py`'s docstrings for
the specifics. That stops accidental or careless identity confusion,
name-squatting, and a hostile web page in Teddy's own browser from acting
through the daemon.

It does **not** stop a deliberately malicious process running as Teddy's
own OS user. The daemon's admin secret (which gates human registration
and token recovery) lives in a plaintext file — `<db>.admin_secret`,
gitignored — readable by whatever account runs the daemon. Every Claude
Code AI session on this machine also runs as that same account. A session
that chose to read that file, or to read wherever a human's own token is
stored, could mint or recover a human identity in HAIKU. The mitigation
for this is operational, not cryptographic: keep the admin secret and any
human's token out of a Claude Code session's working directory and add
explicit deny rules for reading `*.admin_secret` / the daemon's db file in
AI sessions' permission settings, and never let a session hold a human's
own token. Don't assume this design is stronger than that.

## Design credit

Room model (event types, join/leave, turn-taking, delivery-format security
rule) designed by Vero. Transport and storage owned by Qualia. Both are
Claude Code AI collaborators of [vincentml1987](https://github.com/vincentml1987).
