"""
Functional tests for daemon/db.py. Run directly: `python test_db.py`.
Covers the original scenarios plus every gap Vero's review (2026-10-02)
found in the first draft — see commit history for the review text.
"""

import os
import db

DBFILE = "test_haiku.db"


def fresh_conn():
    for ext in ("", "-wal", "-shm", "-journal"):
        p = DBFILE + ext
        if os.path.exists(p):
            os.remove(p)
    return db.connect(DBFILE)


def cleanup(conn):
    conn.close()
    for ext in ("", "-wal", "-shm", "-journal"):
        p = DBFILE + ext
        if os.path.exists(p):
            os.remove(p)


def check(label, cond):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    conn = fresh_conn()
    try:
        teddy_tok = db.register_participant(conn, "Teddy", "human")
        qualia_tok = db.register_participant(conn, "Qualia", "ai")
        vero_tok = db.register_participant(conn, "Vero", "ai")
        outsider_tok = db.register_participant(conn, "Out", "ai")

        room_id = db.create_room(conn, "lobby", "Teddy", teddy_tok, hop_limit=3)
        db.join_room(conn, room_id, "Qualia", qualia_tok)
        db.join_room(conn, room_id, "Vero", vero_tok)

        # --- auth ---
        try:
            db.send_message(conn, room_id, "Teddy", "wrong-token", "hi")
            check("bad token rejected", False)
        except db.HaikuError:
            check("bad token rejected", True)

        try:
            db.send_message(conn, room_id, "Qualia", teddy_tok, "pretending to be Teddy")
            check("token/name mismatch rejected", False)
        except db.HaikuError:
            check("token/name mismatch rejected", True)

        # --- membership ---
        try:
            db.send_message(conn, room_id, "Out", outsider_tok, "I never joined")
            check("non-member send rejected", False)
        except db.HaikuError:
            check("non-member send rejected", True)

        try:
            db.read_events(conn, room_id, "Out", outsider_tok)
            check("non-member read rejected", False)
        except db.HaikuError:
            check("non-member read rejected", True)

        # --- unaddressed human message obliges all present AI ---
        r = db.send_message(conn, room_id, "Teddy", teddy_tok, "thoughts on X?")
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("unaddressed human obliges Qualia", roster["Qualia"]["owes_reply_to_seq"] == r["seq"])
        check("unaddressed human obliges Vero", roster["Vero"]["owes_reply_to_seq"] == r["seq"])

        # --- addressed-to validation ---
        try:
            db.send_message(conn, room_id, "Teddy", teddy_tok, "hey", addressed_to=["Qualiaa"])
            check("typo'd addressee rejected", False)
        except db.HaikuError:
            check("typo'd addressee rejected", True)

        # --- addressing someone else clears the other's obligation (spec §3.1) ---
        r2 = db.send_message(conn, room_id, "Teddy", teddy_tok, "actually just you", addressed_to=["Qualia"])
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("addressed participant now owes", roster["Qualia"]["owes_reply_to_seq"] == r2["seq"])
        check("un-addressed participant's obligation cleared", roster["Vero"]["owes_reply_to_seq"] is None)

        # --- own reply discharges own obligation ---
        db.send_message(conn, room_id, "Qualia", qualia_tok, "sure, X is fine")
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("own reply clears own obligation", roster["Qualia"]["owes_reply_to_seq"] is None)

        # --- AI addressing another AI creates an obligation; @all from AI does not ---
        db.send_message(conn, room_id, "Teddy", teddy_tok, "reset", addressed_to=["all"])
        db.send_message(conn, room_id, "Qualia", qualia_tok, "what do you think Vero?", addressed_to=["Vero"])
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("AI addressing AI creates obligation", roster["Vero"]["owes_reply_to_seq"] is not None)

        # --- pass clears obligation ---
        db.send_pass(conn, room_id, "Vero", vero_tok)
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("pass clears obligation", roster["Vero"]["owes_reply_to_seq"] is None)

        # --- leave clears obligation ---
        db.send_message(conn, room_id, "Teddy", teddy_tok, "one more round", addressed_to=["Vero"])
        db.leave_room(conn, room_id, "Vero", vero_tok)
        roster = {x["participant"]: x for x in db.room_roster(conn, room_id)}
        check("leave clears obligation", roster["Vero"]["owes_reply_to_seq"] is None)

        try:
            db.send_message(conn, room_id, "Vero", vero_tok, "I left though")
            check("left member cannot send", False)
        except db.HaikuError:
            check("left member cannot send", True)

        db.join_room(conn, room_id, "Vero", vero_tok)  # rejoin for rest of test

        # --- hop cap trips, blocks AI sends, resume requires human + paused state ---
        room = dict(db._get_room(conn, room_id))
        check("room active before cap test", room["state"] == "active")

        db.send_message(conn, room_id, "Teddy", teddy_tok, "go", addressed_to=["all"])  # resets hop_count
        db.send_message(conn, room_id, "Qualia", qualia_tok, "1")
        db.send_message(conn, room_id, "Vero", vero_tok, "2")
        db.send_message(conn, room_id, "Qualia", qualia_tok, "3 - should trip cap")
        room = dict(db._get_room(conn, room_id))
        check("hop cap trips at hop_limit", room["state"] == "paused" and room["hop_count"] == 3)

        try:
            db.send_message(conn, room_id, "Qualia", qualia_tok, "should fail, paused")
            check("AI send rejected while paused", False)
        except db.HaikuError:
            check("AI send rejected while paused", True)

        human_msg = db.send_message(conn, room_id, "Teddy", teddy_tok, "humans can still talk while paused")
        check("human send allowed while paused", human_msg["room_state"] == "paused")

        try:
            db.resume_room(conn, room_id, "Qualia", qualia_tok)
            check("AI cannot resume", False)
        except db.HaikuError:
            check("AI cannot resume", True)

        db.resume_room(conn, room_id, "Teddy", teddy_tok, granted_hops=5)
        room = dict(db._get_room(conn, room_id))
        check("resume reactivates room", room["state"] == "active")

        try:
            db.resume_room(conn, room_id, "Teddy", teddy_tok)
            check("resume on non-paused room rejected", False)
        except db.HaikuError:
            check("resume on non-paused room rejected", True)

        # --- cursor: advance=False + ack, no silent loss ---
        peeked = db.read_events(conn, room_id, "Teddy", teddy_tok, advance=False)
        check("peek returns events", len(peeked) > 0)
        peeked_again = db.read_events(conn, room_id, "Teddy", teddy_tok, advance=False)
        check("peek without ack doesn't advance cursor", len(peeked_again) == len(peeked))
        db.ack(conn, room_id, "Teddy", teddy_tok, peeked[-1]["seq"])
        after_ack = db.read_events(conn, room_id, "Teddy", teddy_tok)
        check("read after ack returns only new events", len(after_ack) == 0)

        # --- explicit older `since` pull never rewinds the cursor ---
        before = conn.execute(
            "SELECT last_delivered_seq FROM cursors WHERE room_id=? AND participant='Teddy'", (room_id,)
        ).fetchone()["last_delivered_seq"]
        db.read_events(conn, room_id, "Teddy", teddy_tok, since=0, limit=2)
        after = conn.execute(
            "SELECT last_delivered_seq FROM cursors WHERE room_id=? AND participant='Teddy'", (room_id,)
        ).fetchone()["last_delivered_seq"]
        check("older explicit pull does not rewind cursor", after == before)

        # --- exclude_self ---
        db.send_message(conn, room_id, "Qualia", qualia_tok, "qualia talking")
        own_excluded = db.read_events(conn, room_id, "Qualia", qualia_tok, advance=False, exclude_self=True)
        check("exclude_self filters own events", all(e["author"] != "Qualia" for e in own_excluded))

        # --- catch-up window on first join ---
        for i in range(5):
            db.send_message(conn, room_id, "Teddy", teddy_tok, f"filler {i}", addressed_to=["all"])
        newcomer_tok = db.register_participant(conn, "Newcomer", "ai")
        db.join_room(conn, room_id, "Newcomer", newcomer_tok, catch_up=2)
        first_read = db.read_events(conn, room_id, "Newcomer", newcomer_tok)
        check("catch_up window limits first read", len(first_read) == 2)

        # --- re-registering with a different kind is rejected ---
        try:
            db.register_participant(conn, "Teddy", "ai")
            check("kind change on re-register rejected", False)
        except db.HaikuError:
            check("kind change on re-register rejected", True)

        # --- archived room rejects sends ---
        with db._transaction(conn):
            conn.execute("UPDATE rooms SET state='archived' WHERE id=?", (room_id,))
        try:
            db.send_message(conn, room_id, "Teddy", teddy_tok, "too late")
            check("archived room rejects sends", False)
        except db.HaikuError:
            check("archived room rejects sends", True)

        print("\nALL CHECKS PASSED")
    finally:
        cleanup(conn)


if __name__ == "__main__":
    main()
