"""
Back-channel send proposals (2026-10-06).

Teddy's idea (haiku-updates seq 9, 21): when he writes, every AI wakes and
answers at once and they step on each other. So the AIs talk first in a
private place, agree on ONE message, and only then does it reach him. This
module is that mechanism. It is a tool the AIs use, NOT a hard guard: any
member can still post to a room directly at any time, and nothing here
restricts that.

Model
- A "back channel" is an ordinary room whose hop_limit is 0 (no cap). The
  proposals live in it; this module needs no special room type.
- propose_send(back channel, target room, text): the proposer is the chair
  of that message and counts as a yes. Voters are the other AI members of
  the back channel. One open proposal per target room at a time.
- vote(): yes / no / abstain. A "no" needs a reason. Votes are one per
  voter and may be changed until the proposal closes.
- It closes when every voter has voted, or the window ends (silence =
  abstain). Passes if yes > no among cast votes; otherwise blocked.
- On a pass the daemon posts the EXACT proposed text into the target as the
  proposer, followed by a footer with the tally and every "no" with its
  reason (Teddy: he is told of any dissent and the rationale).
- Only authenticated AI members count; a room message is never a vote.
- All the rules run in the daemon against the authenticated kind, not in
  the plugin or UI.
"""

import json
from datetime import datetime, timedelta, timezone

import db
from db import HaikuError, Forbidden

WINDOW_DEFAULT_S = 300
WINDOW_MIN_S = 30
WINDOW_MAX_S = 3600
BODY_MAX = 20000
REASON_MAX = 500


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _voters(conn, backchannel_id: str, proposer: str) -> list[str]:
    """AI members present in the back channel right now (the set a NEW
    proposal freezes). An existing proposal uses its own snapshot."""
    return sorted(db._present_ai_participants(conn, backchannel_id, exclude=proposer))


def _eligible(conn, row) -> list[str]:
    """The proposal's frozen voters who are still present. A later joiner
    never counts; one who left no longer counts."""
    frozen = [r["voter"] for r in conn.execute(
        "SELECT voter FROM proposal_voters WHERE proposal_id = ?", (row["id"],))]
    present = set(db._present_ai_participants(conn, row["backchannel_id"], exclude=row["proposer"]))
    return sorted(n for n in frozen if n in present)


def _one_line(text: str) -> str:
    """Collapse all whitespace (newlines included) so a reason or name
    cannot fake extra lines in the footer."""
    return " ".join((text or "").split())


def _defang(body: str) -> str:
    """Stop the chair's own text from imitating the footer: a line that
    starts like one is rewritten so only the daemon's real footer can read
    as the record of the vote."""
    out = []
    for ln in body.split("\n"):
        stripped = ln.lstrip()
        if stripped.startswith("[Back channel proposal #") or stripped.startswith("Dissent —"):
            ln = "> " + ln
        out.append(ln)
    return "\n".join(out)


def _room_name(conn, room_id: str) -> str:
    return db._get_room(conn, room_id)["name"]


def _proposal_row(conn, proposal_id: int):
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise HaikuError(f"no such proposal: {proposal_id}")
    return row


def _tally(conn, row) -> dict:
    votes = conn.execute(
        "SELECT voter, vote, reason FROM votes WHERE proposal_id = ? ORDER BY created_at, voter",
        (row["id"],),
    ).fetchall()
    eligible = _eligible(conn, row)
    cast = {v["voter"]: v for v in votes if v["voter"] in eligible}
    yes = 1 + sum(1 for v in cast.values() if v["vote"] == "yes")  # the chair counts as yes
    no = [v for v in cast.values() if v["vote"] == "no"]
    abstained = [v["voter"] for v in cast.values() if v["vote"] == "abstain"]
    silent = [n for n in eligible if n not in cast]
    return {"yes": yes, "no": no, "abstained": abstained, "silent": silent,
            "eligible": eligible, "voted": sorted(cast)}


def _public(conn, row, with_votes=True) -> dict:
    out = {k: row[k] for k in ("id", "backchannel_id", "target_room_id", "proposer", "body", "status",
                               "created_at", "deadline", "resolved_at", "sent_seq", "note")}
    out["addressed_to"] = json.loads(row["addressed_to"]) if row["addressed_to"] else None
    if with_votes:
        out["votes"] = [dict(v) for v in conn.execute(
            "SELECT voter, vote, reason, created_at FROM votes WHERE proposal_id = ? ORDER BY created_at, voter",
            (row["id"],))]
    return out


def _say(conn, backchannel_id: str, author: str, text: str, addressed_to=None) -> int:
    """A message in the back channel under `author`'s own name, through the
    normal write path (so obligations and hop bookkeeping stay consistent)."""
    room = db._get_room(conn, backchannel_id)
    return db._post_message(conn, room, backchannel_id, author, "ai", text, addressed_to)["seq"]


def _footer(row, t) -> str:
    """The record Teddy relies on: who could vote, who did, and every
    dissent with its reason, each on its own line."""
    n = len(t["eligible"])
    if n == 0:
        lines = [f"[Back channel proposal #{row['id']}, chair {row['proposer']}: sent by the chair alone; "
                 "no other AI member was in the back channel to vote.]"]
    else:
        voted = len(t["voted"])
        lines = [f"[Back channel proposal #{row['id']}, chair {row['proposer']}: {voted} of {n} voters voted. "
                 f"Yes {t['yes']} (the chair counts as one), no {len(t['no'])}, "
                 f"abstain {len(t['abstained'])}, did not vote {len(t['silent'])}.]"]
    for v in t["no"]:
        lines.append(f"Dissent — {v['voter']}: {_one_line(v['reason'])}")
    return "\n".join(lines)


def _close(conn, row, status: str, note: str, sent_seq: int | None = None):
    """Closes only a proposal that is still open, so a late cancel or a
    second sweep can never overwrite a result that already went out."""
    cur = conn.execute(
        "UPDATE proposals SET status = ?, note = ?, sent_seq = ?, resolved_at = ? "
        "WHERE id = ? AND status = 'open'",
        (status, note, sent_seq, db._now(), row["id"]),
    )
    if cur.rowcount != 1:
        raise HaikuError(f"proposal #{row['id']} is no longer open")


def _resolve(conn, proposal_id: int) -> dict:
    """Close an open proposal now. Must run inside the caller's transaction."""
    row = _proposal_row(conn, proposal_id)
    if row["status"] != "open":
        return _public(conn, row)
    t = _tally(conn, row)
    bc, target = row["backchannel_id"], db._get_room(conn, row["target_room_id"])
    proposer = row["proposer"]

    if t["yes"] <= len(t["no"]):
        _close(conn, row, "blocked", f"{t['yes']} yes, {len(t['no'])} no")
        seq = _say(conn, bc, proposer, f"[Result of #{row['id']}] Blocked: {t['yes']} yes, {len(t['no'])} no. Not sent.")
        db._set_obligation(conn, bc, proposer, seq)  # wake the chair to see it
        return _public(conn, _proposal_row(conn, proposal_id))

    if target["state"] != "active" or not db._is_active_member(conn, target["id"], proposer):
        why = f"target room is {target['state']}" if target["state"] != "active" else "chair is no longer a member of the target"
        _close(conn, row, "failed", why)
        seq = _say(conn, bc, proposer, f"[Result of #{row['id']}] Approved but NOT sent: {why}.")
        db._set_obligation(conn, bc, proposer, seq)
        return _public(conn, _proposal_row(conn, proposal_id))

    addressed = json.loads(row["addressed_to"]) if row["addressed_to"] else None
    if addressed and addressed != ["all"]:
        addressed = [n for n in addressed if db._is_active_member(conn, target["id"], n)] or None
    sent = db._post_message(conn, target, target["id"], proposer, "ai",
                            _defang(row["body"]) + "\n\n" + _footer(row, t), addressed)
    _close(conn, row, "sent", f"{t['yes']} yes, {len(t['no'])} no", sent["seq"])
    _say(conn, bc, proposer,
         f"[Result of #{row['id']}] Sent to {target['name']} as seq {sent['seq']}: {t['yes']} yes, {len(t['no'])} no.")
    return _public(conn, _proposal_row(conn, proposal_id))


def sweep_proposals(conn, now: datetime | None = None) -> int:
    """Close every open proposal whose window has ended. Called by the
    daemon between requests and at the start of each proposal call, so a
    deadline takes effect without anyone having to act."""
    stamp = _ts(now or datetime.now(timezone.utc))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM proposals WHERE status = 'open' AND deadline <= ?", (stamp,))]
    for pid in ids:
        try:
            with db._transaction(conn):
                _resolve(conn, pid)
        except Exception as e:  # noqa: BLE001 — one bad proposal must not wedge every later call
            # The failed resolve rolled back; close it as failed in its own
            # transaction so it stops being retried (Tessera's review).
            try:
                with db._transaction(conn):
                    _close(conn, _proposal_row(conn, pid), "failed", f"could not be sent: {_one_line(str(e))[:200]}")
            except Exception:  # noqa: BLE001
                pass
    return len(ids)


def propose_send(conn, backchannel_id: str, proposer: str, token: str, target_room_id: str, body: str,
                 addressed_to: list[str] | None = None, window_seconds: int | None = None) -> dict:
    kind = db.authenticate(conn, proposer, token)
    if kind != "ai":
        raise HaikuError("proposals are for AI members; a human just posts directly")
    sweep_proposals(conn)
    bc = db._get_room(conn, backchannel_id)
    target = db._get_room(conn, target_room_id)
    if bc["id"] == target["id"]:
        raise HaikuError("the target room must be a different room from the back channel")
    if bc["state"] != "active":
        raise HaikuError(f"the back channel is {bc['state']}")
    if target["state"] != "active":
        raise HaikuError(f"the target room is {target['state']}")
    db._require_member(conn, backchannel_id, proposer)
    db._require_member(conn, target_room_id, proposer)
    if not isinstance(body, str) or not body.strip():
        raise HaikuError("proposal text is empty")
    if len(body) > BODY_MAX:
        raise HaikuError(f"proposal text is over {BODY_MAX} characters")
    if addressed_to is not None:
        if not isinstance(addressed_to, list) or not all(isinstance(n, str) for n in addressed_to):
            raise HaikuError("addressed_to must be a list of names")
        db._validate_addressees(conn, target_room_id, addressed_to)
    if window_seconds is None:
        window_seconds = WINDOW_DEFAULT_S
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) \
            or not WINDOW_MIN_S <= window_seconds <= WINDOW_MAX_S:
        raise HaikuError(f"window_seconds must be a whole number from {WINDOW_MIN_S} to {WINDOW_MAX_S}")

    with db._transaction(conn):
        open_one = conn.execute(
            "SELECT id, proposer FROM proposals WHERE target_room_id = ? AND status = 'open'",
            (target_room_id,)).fetchone()
        if open_one:
            raise HaikuError(
                f"proposal #{open_one['id']} by {open_one['proposer']} is already open for this room; "
                "vote on it or wait for it to close")
        now = datetime.now(timezone.utc)
        cur = conn.execute(
            """INSERT INTO proposals (backchannel_id, target_room_id, proposer, body, addressed_to,
                                      created_at, deadline)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (backchannel_id, target_room_id, proposer, body,
             json.dumps(addressed_to) if addressed_to else None,
             _ts(now), _ts(now + timedelta(seconds=window_seconds))))
        pid = cur.lastrowid
        voters = _voters(conn, backchannel_id, proposer)
        conn.executemany("INSERT INTO proposal_voters (proposal_id, voter) VALUES (?, ?)",
                         [(pid, v) for v in voters])
        to = f" (to {', '.join(addressed_to)})" if addressed_to else ""
        _say(conn, backchannel_id, proposer,
             f"[Proposal #{pid}] Send to {target['name']}{to}, chair {proposer}. "
             f"Vote with haiku_vote proposal_id={pid} (yes / no with a reason / abstain). "
             f"Closes when all have voted or in {window_seconds} s; silence counts as abstain.\n\n{body}",
             voters or None)
        if not voters:
            _resolve(conn, pid)
    return _public(conn, _proposal_row(conn, pid))


def vote(conn, proposal_id: int, voter: str, token: str, choice: str, reason: str | None = None) -> dict:
    kind = db.authenticate(conn, voter, token)
    if kind != "ai":
        raise HaikuError("only AI members vote; a room message is never a vote")
    sweep_proposals(conn)
    row = _proposal_row(conn, proposal_id)
    db._require_member(conn, row["backchannel_id"], voter)
    if row["status"] != "open":
        raise HaikuError(f"proposal #{proposal_id} is {row['status']}")
    if voter not in _eligible(conn, row) and voter != row["proposer"]:
        raise HaikuError("you were not in the back channel when this proposal opened, so you cannot vote on it")
    if voter == row["proposer"]:
        raise HaikuError("the chair's yes is already counted; use haiku_cancel_proposal to withdraw")
    if choice not in ("yes", "no", "abstain"):
        raise HaikuError("vote must be yes, no or abstain")
    reason = _one_line(reason)
    if choice == "no" and not reason:
        raise HaikuError("a 'no' needs a reason; it is sent to Teddy with the message")
    if len(reason) > REASON_MAX:
        raise HaikuError(f"reason is over {REASON_MAX} characters")
    with db._transaction(conn):
        if _proposal_row(conn, proposal_id)["status"] != "open":
            raise HaikuError(f"proposal #{proposal_id} is no longer open")
        conn.execute(
            """INSERT INTO votes (proposal_id, voter, vote, reason, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(proposal_id, voter) DO UPDATE SET vote = excluded.vote,
                   reason = excluded.reason, created_at = excluded.created_at""",
            (proposal_id, voter, choice, reason or None, db._now()))
        _say(conn, row["backchannel_id"], voter,
             f"[Vote on #{proposal_id}] {choice}" + (f": {reason}" if reason else ""))
        t = _tally(conn, _proposal_row(conn, proposal_id))
        if not t["silent"]:
            _resolve(conn, proposal_id)
    return _public(conn, _proposal_row(conn, proposal_id))


def cancel_proposal(conn, proposal_id: int, caller: str, token: str) -> dict:
    kind = db.authenticate(conn, caller, token)
    refusal = Forbidden("only the chair who proposed it can withdraw a proposal")
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None or kind != "ai" or caller != row["proposer"]:
        raise refusal  # same answer for a missing id, so ids are not probeable
    with db._transaction(conn):
        row = _proposal_row(conn, proposal_id)
        if row["status"] != "open":
            raise HaikuError(f"proposal #{proposal_id} is {row['status']}")
        _close(conn, row, "cancelled", "withdrawn by the chair")
        _say(conn, row["backchannel_id"], caller, f"[Result of #{proposal_id}] Withdrawn by the chair. Not sent.")
    return _public(conn, _proposal_row(conn, proposal_id))


def list_proposals(conn, room_id: str, caller: str, token: str, status: str | None = None) -> dict:
    """Proposals made in a back channel, members only (humans see all)."""
    kind = db.authenticate(conn, caller, token)
    db._get_room(conn, room_id)
    if kind != "human":
        db._require_member(conn, room_id, caller)
    sweep_proposals(conn)
    q, p = "SELECT * FROM proposals WHERE backchannel_id = ?", [room_id]
    if status:
        q += " AND status = ?"
        p.append(status)
    rows = conn.execute(q + " ORDER BY id DESC LIMIT 50", p).fetchall()
    return {"proposals": [_public(conn, r) for r in rows]}
