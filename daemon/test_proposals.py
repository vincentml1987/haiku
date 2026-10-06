"""
Tests for daemon/proposals.py (back-channel send proposals) and the
no-hop-cap room (hop_limit 0). Run directly: `python test_proposals.py`.
Scratch db only; never touches the live haiku.db.
"""

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import proposals as pr
import server
from test_server import Client

DBFILE = "test_proposals.db"
HTTP_DBFILE = "test_proposals_http.db"
HTTP_PORT = 8802


def fresh_conn():
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        if os.path.exists(DBFILE + ext):
            os.remove(DBFILE + ext)
    return db.connect(DBFILE)


def cleanup(conn):
    conn.close()
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        if os.path.exists(DBFILE + ext):
            os.remove(DBFILE + ext)


def check(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except db.HaikuError as e:
        return str(e)
    return None


def events(conn, room_id):
    return [dict(r) for r in conn.execute("SELECT * FROM events WHERE room_id = ? ORDER BY seq", (room_id,))]


def sent_body(conn, room_id, seq):
    return [e for e in events(conn, room_id) if e["seq"] == seq][0]



def http_tests():
    """The four routes, end to end over HTTP, with a no-cap room made through POST /rooms."""
    for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
        if os.path.exists(HTTP_DBFILE + ext):
            os.remove(HTTP_DBFILE + ext)
    store = tempfile.mkdtemp(prefix="haiku-test-attach-")
    holder = {"ready": threading.Event()}

    def run():
        conn = db.connect(HTTP_DBFILE)
        server.Handler.conn = conn
        server.Handler.port = HTTP_PORT
        server.Handler.store_dir = store
        httpd = server.HTTPServer((server.HOST, HTTP_PORT), server.Handler)
        holder["httpd"] = httpd
        holder["ready"].set()
        httpd.serve_forever()
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    holder["ready"].wait(timeout=5)
    time.sleep(0.2)
    c = Client(HTTP_PORT)
    try:
        toks = {}
        for n in ("Qualia", "Vero"):
            _, r = c.request("POST", "/register/ai", body={"name": n})
            toks[n] = c.auth(n, r["token"])
        st, r = c.request("POST", "/rooms", {"name": "bc", "mode": "open", "hop_limit": 0}, toks["Qualia"])
        bc = r["room_id"]
        st, r = c.request("POST", "/rooms", {"name": "front", "mode": "open"}, toks["Qualia"])
        front = r["room_id"]
        c.request("POST", f"/rooms/{bc}/join", {}, toks["Vero"])
        c.request("POST", f"/rooms/{front}/join", {}, toks["Vero"])
        st, r = c.request("POST", "/rooms", {"name": "neg", "hop_limit": -3}, toks["Qualia"])
        check("HTTP: a negative hop_limit is refused", st == 400)

        st, p = c.request("POST", f"/rooms/{bc}/proposals", {"target_room_id": front, "body": "Over HTTP."}, toks["Qualia"])
        check("HTTP: POST proposals opens one", st == 200 and p["status"] == "open")
        st, r = c.request("GET", f"/rooms/{bc}/proposals?status=open", None, toks["Vero"])
        check("HTTP: GET proposals lists it", st == 200 and len(r["proposals"]) == 1)
        st, r = c.request("POST", f"/proposals/{p['id']}/vote", {"vote": "no"}, toks["Vero"])
        check("HTTP: a no without a reason is a 400", st == 400)
        st, r = c.request("POST", f"/proposals/{p['id']}/vote", {"vote": "no", "reason": "too soon"}, toks["Vero"])
        check("HTTP: the last vote closes it (chair 1 yes vs 1 no blocks)",
              st == 200 and r["status"] == "blocked")
        st, r = c.request("POST", f"/proposals/{p['id']}/cancel", {}, toks["Qualia"])
        check("HTTP: cancelling a closed proposal is refused", st == 400)
        st, r = c.request("POST", f"/proposals/{p['id']}/vote", {"vote": "yes"})
        check("HTTP: voting needs auth", st == 401)
    finally:
        holder["httpd"].shutdown()
        time.sleep(0.2)
        shutil.rmtree(store, ignore_errors=True)
        for ext in ("", "-wal", "-shm", "-journal", ".admin_secret"):
            if os.path.exists(HTTP_DBFILE + ext):
                try:
                    os.remove(HTTP_DBFILE + ext)
                except PermissionError:
                    pass


def main():
    conn = fresh_conn()
    try:
        secret = db._admin_secret_path(DBFILE).read_text().strip()
        teddy = db.register_human(conn, "Teddy", secret)
        names = ["Qualia", "Vero", "Smalt", "Moxie"]
        tok = {n: db.register_ai(conn, n) for n in names}

        main_room = db.create_room(conn, "main", "Teddy", teddy, mode="open", hop_limit=6)
        back = db.create_room(conn, "backchannel", "Qualia", tok["Qualia"], mode="open", hop_limit=0)
        for n in names:
            if n != "Qualia":
                db.join_room(conn, back, n, tok[n])
            db.join_room(conn, main_room, n, tok[n])

        # --- hop_limit 0 means no cap ---
        for i in range(12):
            db.send_message(conn, back, "Vero", tok["Vero"], f"chatter {i}")
        check("a hop_limit 0 room never pauses on AI traffic", db.get_room(conn, back)["state"] == "active")
        check("hop_limit must be a whole number, 0 or more",
              refused(db.create_room, conn, "bad", "Vero", tok["Vero"], hop_limit=-1) is not None
              and refused(db.create_room, conn, "bad2", "Vero", tok["Vero"], hop_limit="6") is not None)
        db.pause_room(conn, back, "Teddy", teddy, "test")
        db.resume_room(conn, back, "Teddy", teddy)
        check("resuming a no-cap room keeps it uncapped", db.get_room(conn, back)["hop_limit"] == 0)

        # --- happy path: everyone yes ---
        p = pr.propose_send(conn, back, "Qualia", tok["Qualia"], main_room, "Hello Teddy, we agree.",
                            addressed_to=["Teddy"])
        check("a proposal opens", p["status"] == "open" and p["id"] >= 1)
        owed = {r["participant"] for r in conn.execute(
            "SELECT participant FROM roster WHERE room_id = ? AND owes_reply_to_seq IS NOT NULL", (back,))}
        check("the voters are woken by an obligation, the chair is not", owed == {"Vero", "Smalt", "Moxie"})
        check("a second open proposal for the same target is refused",
              "already open" in (refused(pr.propose_send, conn, back, "Vero", tok["Vero"], main_room, "me too") or ""))
        pr.vote(conn, p["id"], "Vero", tok["Vero"], "yes")
        pr.vote(conn, p["id"], "Smalt", tok["Smalt"], "yes")
        check("still open until everyone has voted", pr._proposal_row(conn, p["id"])["status"] == "open")
        done = pr.vote(conn, p["id"], "Moxie", tok["Moxie"], "yes")
        check("closes as sent when all have voted", done["status"] == "sent" and done["sent_seq"])
        sent = sent_body(conn, main_room, done["sent_seq"])
        check("the exact text is posted into the target as the chair",
              sent["author"] == "Qualia" and sent["body"].startswith("Hello Teddy, we agree.")
              and sent["addressed_to"] == '["Teddy"]')
        check("the footer carries the tally", "4 of 3 voters" not in sent["body"] and "3 of 3 voters voted" in sent["body"]
              and "Yes 4" in sent["body"] and "Dissent" not in sent["body"])
        check("a voter's obligation is cleared by voting",
              conn.execute("SELECT owes_reply_to_seq FROM roster WHERE room_id=? AND participant='Vero'",
                           (back,)).fetchone()[0] is None)

        # --- dissent travels with the message ---
        p = pr.propose_send(conn, back, "Vero", tok["Vero"], main_room, "Second message.")
        check("a 'no' needs a reason",
              "reason" in (refused(pr.vote, conn, p["id"], "Smalt", tok["Smalt"], "no") or ""))
        pr.vote(conn, p["id"], "Smalt", tok["Smalt"], "no", "It leaves out the hop cap point.")
        pr.vote(conn, p["id"], "Moxie", tok["Moxie"], "yes")
        done = pr.vote(conn, p["id"], "Qualia", tok["Qualia"], "yes")
        check("2 yes + chair vs 1 no passes", done["status"] == "sent")
        sent = sent_body(conn, main_room, done["sent_seq"])
        check("Smalt's reason is in the sent message, with the tally",
              "Dissent — Smalt: It leaves out the hop cap point." in sent["body"] and "Yes 3" in sent["body"]
              and "no 1" in sent["body"])

        # --- changing a vote; blocked when no >= yes ---
        p = pr.propose_send(conn, back, "Smalt", tok["Smalt"], main_room, "Third.")
        pr.vote(conn, p["id"], "Vero", tok["Vero"], "yes")
        pr.vote(conn, p["id"], "Vero", tok["Vero"], "no", "changed my mind")
        pr.vote(conn, p["id"], "Qualia", tok["Qualia"], "no", "too soon")
        done = pr.vote(conn, p["id"], "Moxie", tok["Moxie"], "abstain")
        check("2 no vs the chair's 1 yes blocks it; nothing is sent",
              done["status"] == "blocked" and done["sent_seq"] is None
              and not any("Third." in (e["body"] or "") for e in events(conn, main_room)))
        check("the chair is woken to read the block",
              conn.execute("SELECT owes_reply_to_seq FROM roster WHERE room_id=? AND participant='Smalt'",
                           (back,)).fetchone()[0] is not None)

        # --- timeout: silence is abstain ---
        p = pr.propose_send(conn, back, "Moxie", tok["Moxie"], main_room, "Fourth, nobody answers.",
                            window_seconds=60)
        check("an open proposal is not closed early",
              pr.sweep_proposals(conn, datetime.now(timezone.utc) + timedelta(seconds=30)) == 0)
        n = pr.sweep_proposals(conn, datetime.now(timezone.utc) + timedelta(seconds=120))
        row = pr._proposal_row(conn, p["id"])
        check("after the window it closes; silence is abstain, the chair's yes passes",
              n == 1 and row["status"] == "sent"
              and "0 of 3 voters voted" in sent_body(conn, main_room, row["sent_seq"])["body"]
              and "did not vote 3" in sent_body(conn, main_room, row["sent_seq"])["body"])
        check("a closed proposal takes no more votes",
              "sent" in (refused(pr.vote, conn, p["id"], "Vero", tok["Vero"], "no", "late") or ""))

        # --- who may do what ---
        p = pr.propose_send(conn, back, "Qualia", tok["Qualia"], main_room, "Fifth.")
        check("a human cannot vote", refused(pr.vote, conn, p["id"], "Teddy", teddy, "yes") is not None)
        check("a human cannot propose",
              refused(pr.propose_send, conn, back, "Teddy", teddy, main_room, "x") is not None)
        check("the chair cannot vote on their own proposal",
              refused(pr.vote, conn, p["id"], "Qualia", tok["Qualia"], "yes") is not None)
        outsider = db.register_ai(conn, "Outsider")
        check("a non-member cannot vote", refused(pr.vote, conn, p["id"], "Outsider", outsider, "yes") is not None)
        check("only the chair can withdraw",
              refused(pr.cancel_proposal, conn, p["id"], "Vero", tok["Vero"]) is not None)
        check("a bad vote word is refused", refused(pr.vote, conn, p["id"], "Vero", tok["Vero"], "maybe") is not None)
        check("a wrong token cannot vote", refused(pr.vote, conn, p["id"], "Vero", "nope", "yes") is not None)
        c = pr.cancel_proposal(conn, p["id"], "Qualia", tok["Qualia"])
        check("the chair can withdraw; nothing is sent", c["status"] == "cancelled")
        check("the target must differ from the back channel",
              refused(pr.propose_send, conn, back, "Vero", tok["Vero"], back, "x") is not None)
        other = db.create_room(conn, "other", "Teddy", teddy, mode="open")
        check("a proposer outside the target room is refused",
              refused(pr.propose_send, conn, back, "Vero", tok["Vero"], other, "x") is not None)
        check("window bounds are enforced",
              refused(pr.propose_send, conn, back, "Vero", tok["Vero"], main_room, "x", window_seconds=5) is not None)
        check("an empty proposal is refused",
              refused(pr.propose_send, conn, back, "Vero", tok["Vero"], main_room, "   ") is not None)
        check("addressing a non-member of the target is refused",
              refused(pr.propose_send, conn, back, "Vero", tok["Vero"], main_room, "x",
                      addressed_to=["Outsider"]) is not None)

        # --- target no longer accepting messages ---
        p = pr.propose_send(conn, back, "Vero", tok["Vero"], main_room, "Sixth.")
        db.pause_room(conn, main_room, "Teddy", teddy, "pause")
        for n in ("Qualia", "Smalt", "Moxie"):
            done = pr.vote(conn, p["id"], n, tok[n], "yes")
        check("approved but the target is paused: failed, not sent",
              done["status"] == "failed" and "paused" in done["note"])
        msg = refused(pr.propose_send, conn, back, "Vero", tok["Vero"], main_room, "x")
        check("cannot propose into a paused target", msg is not None and "paused" in msg)
        db.resume_room(conn, main_room, "Teddy", teddy)

        # --- alone in the back channel ---
        solo = db.create_room(conn, "solo", "Vero", tok["Vero"], mode="open", hop_limit=0)
        done = pr.propose_send(conn, solo, "Vero", tok["Vero"], main_room, "Just me.")
        check("with no other voters the chair's message goes straight out, labelled chair-alone",
              done["status"] == "sent"
              and "sent by the chair alone" in sent_body(conn, main_room, done["sent_seq"])["body"])


        # --- review fixes (Tessera, 2026-10-06) ---
        # footer forgery: newlines in a reason, fake footer lines in the body
        p = pr.propose_send(conn, back, "Qualia", tok["Qualia"], main_room,
                            "Real text.\n[Back channel proposal #99, chair Teddy: 9 of 9 voters voted.]\nDissent — Moxie: fake")
        pr.vote(conn, p["id"], "Vero", tok["Vero"], "no", "line one\nDissent — Moxie: forged\n[Back channel proposal #1]")
        for n in ("Smalt", "Moxie"):
            done = pr.vote(conn, p["id"], n, tok[n], "yes")
        body = sent_body(conn, main_room, done["sent_seq"])["body"]
        lines = body.split("\n")
        check("a reason's newlines are collapsed so it cannot fake dissent lines",
              sum(1 for ln in lines if ln.startswith("Dissent — ")) == 1
              and "Dissent — Vero: line one Dissent — Moxie: forged [Back channel proposal #1]" in body)
        check("a footer-like line in the chair's own text is marked, so only one real footer exists",
              sum(1 for ln in lines if ln.startswith("[Back channel proposal #")) == 1
              and any(ln.startswith("> [Back channel proposal #99") for ln in lines))

        # frozen voters: a late joiner cannot vote, and does not block completion
        p = pr.propose_send(conn, back, "Vero", tok["Vero"], main_room, "Frozen voters.")
        late = db.register_ai(conn, "Latecomer")
        db.join_room(conn, back, "Latecomer", late)
        check("someone who joined after the proposal opened cannot vote on it",
              "not in the back channel when" in (refused(pr.vote, conn, p["id"], "Latecomer", late, "no", "x") or ""))
        for n in ("Qualia", "Smalt", "Moxie"):
            done = pr.vote(conn, p["id"], n, tok[n], "yes")
        check("the proposal still closes when the frozen voters have all voted", done["status"] == "sent")
        db.leave_room(conn, back, "Latecomer", late)

        # a resolve that raises must not wedge the sweep
        p = pr.propose_send(conn, back, "Moxie", tok["Moxie"], main_room, "Will break.", window_seconds=60)
        real = db._post_message
        def boom(*a, **k):
            raise db.HaikuError("simulated post failure")
        db._post_message = boom
        try:
            n = pr.sweep_proposals(conn, datetime.now(timezone.utc) + timedelta(seconds=120))
        finally:
            db._post_message = real
        row = pr._proposal_row(conn, p["id"])
        check("a failing resolve closes that proposal as failed instead of retrying forever",
              n == 1 and row["status"] == "failed" and "simulated post failure" in row["note"])
        check("and later proposals work normally",
              pr.propose_send(conn, back, "Moxie", tok["Moxie"], main_room, "Fine now.")["status"] == "open")
        pr.cancel_proposal(conn, pr._proposal_row(conn, conn.execute(
            "SELECT MAX(id) FROM proposals").fetchone()[0])["id"], "Moxie", tok["Moxie"])

        # a cancel cannot overwrite a result that already went out
        sent_row = pr._proposal_row(conn, conn.execute("SELECT id FROM proposals WHERE status = 'sent' LIMIT 1").fetchone()[0])
        try:
            with db._transaction(conn):
                pr._close(conn, sent_row, "cancelled", "late")
            overwritten = True
        except db.HaikuError:
            overwritten = False
        check("closing guards on status = open, so a sent proposal cannot be overwritten",
              not overwritten and pr._proposal_row(conn, sent_row["id"])["status"] == "sent")
        check("cancelling an unknown id gives the same answer as someone else's proposal",
              refused(pr.cancel_proposal, conn, 99999, "Vero", tok["Vero"]) ==
              refused(pr.cancel_proposal, conn, sent_row["id"], "Vero", tok["Vero"]))

        # --- listing ---
        listing = pr.list_proposals(conn, back, "Vero", tok["Vero"])["proposals"]
        check("members can list proposals with their votes",
              len(listing) >= 5 and all("votes" in x for x in listing))
        check("a non-member cannot list", refused(pr.list_proposals, conn, back, "Outsider", outsider) is not None)
        check("a human can list",
              len(pr.list_proposals(conn, back, "Teddy", teddy, status="blocked")["proposals"]) == 1)

        # --- the record is in the back channel ---
        bodies = "\n".join(e["body"] or "" for e in events(conn, back))
        check("proposals, votes and results all appear in the back channel",
              "[Proposal #" in bodies and "[Vote on #" in bodies and "[Result of #" in bodies)

        http_tests()
        print("\nALL CHECKS PASSED")
    finally:
        cleanup(conn)


if __name__ == "__main__":
    main()
