"""Proposed meetings, invitations, answers, couriers and attendance.

Nobody is ever made to attend. An invitation only exists because someone asked someone to come; it is only
delivered when the invitee was told (in person, by whoever carried it, or by a courier); and only the invitee's
own words answer it: accepted, accepted with conditions, declined, or a counter-proposal. A meeting is agreed only
when every invitee has accepted (with or without conditions), and convened only when the people are actually
there together at the venue in the game. Accepting is a promise to attend; not turning up breaks it.
"""
from __future__ import annotations

import re

from livingworld.diplomacy import commitments as C
from livingworld.diplomacy import common as K

UNDECIDED = "undecided"
COURIER_DAYS = 2.0
_COURIER = re.compile(r"\b(courier|messenger|runner|rider|letter|send word|send a message|write to|my reply|"
                      r"carry (?:my|the) (?:answer|reply|word))\b", re.I)
_RESPONSE_STATUS = {"accepted": "accepted", "conditional": "conditional", "declined": "declined",
                    "countered": "countered"}


def venue_key(venue: str | None) -> str:
    v = re.sub(r"[^a-z0-9 ]+", "", (venue or "").lower()).strip()
    v = re.sub(r"^(the|at|in)\s+", "", v)
    return v or UNDECIDED


def open_meeting(db, venue: str | None, involving: list[int]) -> dict | None:
    """The newest proposed or agreed meeting at this venue (or, with no venue, one these people are part of)."""
    key = venue_key(venue)
    if key != UNDECIDED:
        row = db.execute("SELECT * FROM v_dip_meeting WHERE venue_key = ? AND status IN ('proposed','agreed')"
                         " ORDER BY proposed_gt DESC, meeting_id DESC LIMIT 1", (key,)).fetchone()
        return dict(row) if row else None
    for npc_id in involving:
        row = db.execute(
            "SELECT m.* FROM v_dip_meeting m WHERE m.status IN ('proposed','agreed') AND (m.proposed_by_id = ?"
            " OR m.host_id = ? OR EXISTS (SELECT 1 FROM dip_invitation i WHERE i.meeting_id = m.meeting_id"
            " AND (i.invitee_id = ? OR i.invited_by_id = ?)))"
            " ORDER BY m.proposed_gt DESC, m.meeting_id DESC LIMIT 1", (npc_id, npc_id, npc_id, npc_id)).fetchone()
        if row:
            return dict(row)
    return None


def propose(db, timeline: int, venue: str | None, proposed_by: int, gt: float, stmt_id: int | None,
            purpose: str = "", host: int | None = None) -> dict:
    """An open meeting at this venue, or a new proposed one."""
    found = open_meeting(db, venue, [proposed_by])
    if found:
        return found
    meeting_id = db.execute(
        "INSERT INTO dip_meeting (timeline_id, venue, venue_key, purpose, proposed_by_id, host_id, stmt_id, proposed_gt)"
        " VALUES (?,?,?,?,?,?,?,?)", (timeline, (venue or "a place not yet agreed").strip()[:60], venue_key(venue),
                                      purpose[:160], proposed_by, host, stmt_id, gt)).lastrowid
    _status(db, timeline, meeting_id, "proposed", f"proposed by {K.name_of(db, proposed_by)}", gt)
    return dict(db.execute("SELECT * FROM v_dip_meeting WHERE meeting_id = ?", (meeting_id,)).fetchone())


def _status(db, timeline: int, meeting_id: int, status: str, reason: str, gt: float, conv_id: int | None = None) -> None:
    db.execute("INSERT INTO dip_meeting_change (timeline_id, meeting_id, status, conv_id, reason, game_time)"
               " VALUES (?,?,?,?,?,?)", (timeline, meeting_id, status, conv_id, reason, gt))
    K.event(db, timeline, "diplomacy.meeting", {"key": f"meeting:{meeting_id}:{status}:{conv_id or ''}:{gt:.4f}",
                                               "meeting_id": meeting_id, "status": status}, gt)


def invitation(db, meeting_id: int, invitee: int) -> dict | None:
    row = db.execute("SELECT * FROM v_dip_invitation WHERE meeting_id = ? AND invitee_id = ?",
                     (meeting_id, invitee)).fetchone()
    return dict(row) if row else None


def _inv_change(db, timeline: int, inv_id: int, status: str, gt: float, stmt_id: int | None = None,
                conditions: str | None = None, counter_venue: str | None = None) -> None:
    db.execute("INSERT INTO dip_invitation_change (timeline_id, inv_id, status, conditions, counter_venue, stmt_id,"
               " game_time) VALUES (?,?,?,?,?,?,?)", (timeline, inv_id, status, conditions, counter_venue, stmt_id, gt))
    K.event(db, timeline, "diplomacy.invitation", {"key": f"inv:{inv_id}:{status}:{stmt_id or ''}", "inv_id": inv_id,
                                                  "status": status}, gt)


def invite(db, timeline: int, meeting: dict, invitee: int, invited_by: int, stmt_id: int | None, gt: float,
           delivered: bool) -> dict:
    inv = invitation(db, meeting["meeting_id"], invitee)
    if inv is None:
        inv_id = db.execute("INSERT INTO dip_invitation (timeline_id, meeting_id, invitee_id, invited_by_id, stmt_id,"
                            " invited_gt) VALUES (?,?,?,?,?,?)",
                            (timeline, meeting["meeting_id"], invitee, invited_by, stmt_id, gt)).lastrowid
        _inv_change(db, timeline, inv_id, "delivered" if delivered else "pending", gt, stmt_id)
    elif delivered and inv["status"] == "pending":
        _inv_change(db, timeline, inv["inv_id"], "delivered", gt, stmt_id)
    return invitation(db, meeting["meeting_id"], invitee)


def reassess(db, timeline: int, meeting_id: int, gt: float) -> str | None:
    """Agreed when every invitee has accepted (with or without conditions); abandoned when every invitation was
    declined or withdrawn; back to proposed when an agreed meeting loses someone. Never convened here."""
    m = db.execute("SELECT * FROM v_dip_meeting WHERE meeting_id = ?", (meeting_id,)).fetchone()
    if m is None or m["status"] not in ("proposed", "agreed"):
        return None
    statuses = [r[0] for r in db.execute("SELECT status FROM v_dip_invitation WHERE meeting_id = ?", (meeting_id,))]
    if not statuses:
        return None
    if all(s in ("accepted", "conditional") for s in statuses):
        new = "agreed"
    elif all(s in ("declined", "withdrawn") for s in statuses):
        new = "abandoned"
    else:
        new = "proposed"
    if new != m["status"]:
        _status(db, timeline, meeting_id, new, "the invitees' answers so far", gt)
        return new
    return None


def on_statement(db, timeline: int, stmt: dict, present: list[int], line_text: str) -> str | None:
    """Invitations, answers and venues from one recorded statement. Returns what changed, for the log."""
    kind, gt = stmt["kind"], stmt["game_time"]
    whose, said_by, target = stmt["subject_id"], stmt["speaker_id"], stmt["target_id"]
    first_hand = whose == said_by and stmt["about_name"] is None
    if kind == "invitation" and target is not None:
        if first_hand:
            meeting = propose(db, timeline, stmt["venue"], whose, gt, stmt["stmt_id"], stmt["summary"])
            inv = invite(db, timeline, meeting, target, whose, stmt["stmt_id"], gt, target in present)
            reassess(db, timeline, meeting["meeting_id"], gt)
            return f"invitation {inv['inv_id']} ({inv['status']})"
        # carrying someone else's invitation: deliver theirs if they made one, otherwise the carrier is inviting
        for row in db.execute("SELECT * FROM v_dip_invitation WHERE invitee_id = ? AND invited_by_id = ?"
                              " AND status = 'pending' ORDER BY invited_gt DESC", (target, whose)).fetchall():
            if target in present:
                _inv_change(db, timeline, row["inv_id"], "delivered", gt, stmt["stmt_id"])
                return f"delivered invitation {row['inv_id']} on behalf of {K.name_of(db, whose)}"
            return None
        if target in present:
            meeting = propose(db, timeline, stmt["venue"], said_by, gt, stmt["stmt_id"], stmt["summary"])
            inv = invite(db, timeline, meeting, target, said_by, stmt["stmt_id"], gt, True)
            reassess(db, timeline, meeting["meeting_id"], gt)
            return f"invitation {inv['inv_id']} in {K.name_of(db, whose)}'s name, from {K.name_of(db, said_by)}"
        return None
    if kind in ("meeting_location", "proposal") and first_hand and (stmt["issue"] == "meeting" or kind == "meeting_location") \
            and stmt["venue"]:
        meeting = propose(db, timeline, stmt["venue"], whose, gt, stmt["stmt_id"], stmt["summary"])
        return f"meeting {meeting['meeting_id']} at {meeting['venue']}"
    if kind == "invitation_response" and first_hand and stmt["response"]:
        return _answer(db, timeline, stmt, present, line_text)
    if kind == "retraction" and first_hand and stmt["issue"] == "meeting":
        n = 0
        for row in db.execute("SELECT * FROM v_dip_invitation WHERE invited_by_id = ? AND status NOT IN"
                              " ('withdrawn','declined')", (whose,)).fetchall():
            _inv_change(db, timeline, row["inv_id"], "withdrawn", gt, stmt["stmt_id"])
            reassess(db, timeline, row["meeting_id"], gt)
            n += 1
        return f"withdrew {n} invitation(s)" if n else None
    return None


def _answer(db, timeline: int, stmt: dict, present: list[int], line_text: str) -> str | None:
    whose, gt = stmt["subject_id"], stmt["game_time"]
    rows = db.execute("SELECT i.*, m.venue_key, m.venue FROM v_dip_invitation i JOIN v_dip_meeting m"
                      " ON m.meeting_id = i.meeting_id WHERE i.invitee_id = ? AND i.status <> 'withdrawn'"
                      " AND m.status IN ('proposed','agreed') ORDER BY i.invited_gt DESC, i.inv_id DESC",
                      (whose,)).fetchall()
    if not rows:
        return None
    key = venue_key(stmt["venue"])
    inv = next((r for r in rows if r["venue_key"] == key), rows[0])
    status = _RESPONSE_STATUS[stmt["response"]]
    counter = stmt["venue"] if status == "countered" and key not in (UNDECIDED, inv["venue_key"]) else None
    _inv_change(db, timeline, inv["inv_id"], status, gt, stmt["stmt_id"], stmt["condition"], counter)
    existing = C.latest_open(db, whose, meeting_id=inv["meeting_id"])
    if status in ("accepted", "conditional") and existing is None:
        C.make(db, timeline, stmt["stmt_id"], whose, inv["invited_by_id"], "meeting",
               f"would attend the meeting at {inv['venue']}", stmt["condition"], stmt["due_days"], stmt["audience"],
               gt, inv["meeting_id"])
    elif status in ("declined", "countered") and existing is not None:
        C.set_status(db, timeline, existing, "withdrawn", "changed their answer", gt, stmt["stmt_id"])
    reassess(db, timeline, inv["meeting_id"], gt)
    inviter = inv["invited_by_id"]
    if inviter not in present and _COURIER.search(line_text or ""):
        schedule_courier(db, timeline, stmt["stmt_id"], whose, inviter, gt)
    return f"{K.name_of(db, whose)} {status} invitation {inv['inv_id']}"


def schedule_courier(db, timeline: int, stmt_id: int, from_id: int, to_id: int, gt: float) -> None:
    db.execute("INSERT OR IGNORE INTO dip_followup (timeline_id, key, kind, stmt_id, from_id, to_id, due_gt, created_gt)"
               " VALUES (?,?, 'courier', ?,?,?,?,?)", (timeline, f"courier:{stmt_id}:{to_id}", stmt_id, from_id, to_id,
                                                     gt + COURIER_DAYS, gt))


def deliver_couriers(db, now: float) -> int:
    """Couriers whose travel time has passed arrive: the recipient learns the answer (origin 'courier')."""
    timeline = K.active_timeline(db)
    n = 0
    for f in db.execute("SELECT * FROM v_dip_followup WHERE kind = 'courier' AND due_gt <= ? ORDER BY due_gt, fu_id",
                        (now,)).fetchall():
        if K.event_exists(db, "diplomacy.courier", f["key"]):
            continue
        event_id = K.event(db, timeline, "diplomacy.courier", {"key": f["key"], "stmt_id": f["stmt_id"],
                                                               "from": f["from_id"], "to": f["to_id"]}, f["due_gt"])
        db.execute("INSERT OR IGNORE INTO dip_awareness (timeline_id, holder_id, stmt_id, origin, told_by_id, confidence,"
                   " learned_gt) VALUES (?,?,?, 'courier', ?, 0.9, ?)",
                   (timeline, f["to_id"], f["stmt_id"], f["from_id"], f["due_gt"]))
        n += 1 if event_id else 0
    return n


def parties(db, meeting: dict) -> set[int]:
    """Who is expected: the host or proposer (unless it is the player, who is never 'expected') and every invitee
    who accepted."""
    out = {r[0] for r in db.execute("SELECT invitee_id FROM v_dip_invitation WHERE meeting_id = ? AND status IN"
                                    " ('accepted','conditional')", (meeting["meeting_id"],))}
    for who in (meeting.get("host_id"), meeting.get("proposed_by_id")):
        if who is not None and not K.is_player(db, who):
            out.add(who)
    return out


def convene(db, conv_id: int, location: str | None, present: list[int], now: float) -> list[int]:
    """An agreed meeting convenes when two or more of its expected parties are together at its venue."""
    if not location:
        return []
    here = venue_key(location)
    timeline = K.active_timeline(db)
    out = []
    for m in db.execute("SELECT * FROM v_dip_meeting WHERE status = 'agreed'").fetchall():
        m = dict(m)
        if m["venue_key"] == UNDECIDED or (m["venue_key"] not in here and here not in m["venue_key"]):
            continue
        expected = parties(db, m)
        there = expected & set(present)
        if len(there) < 2:
            continue
        _status(db, timeline, m["meeting_id"], "convened", "the parties met at the venue", now, conv_id)
        for npc_id in sorted(expected):
            db.execute("INSERT OR IGNORE INTO dip_attendance (timeline_id, meeting_id, npc_id, status, conv_id, game_time)"
                       " VALUES (?,?,?,?,?,?)", (timeline, m["meeting_id"], npc_id,
                                                 "present" if npc_id in there else "absent", conv_id, now))
        out.append(m["meeting_id"])
    return out


def conclude(db, current_conv: int | None) -> list[int]:
    """A convened meeting whose conversation has ended concludes: attendees kept their word, absentees broke it."""
    timeline = K.active_timeline(db)
    out = []
    for m in db.execute("SELECT * FROM v_dip_meeting WHERE status = 'convened'").fetchall():
        conv = m["status_conv_id"]
        if conv is None or conv == current_conv:
            continue
        ended = db.execute("SELECT ended_gt FROM conversation WHERE conv_id = ?", (conv,)).fetchone()
        if ended is None or ended[0] is None:
            continue
        gt = ended[0]
        _status(db, timeline, m["meeting_id"], "concluded", "the meeting ended", gt, conv)
        for a in db.execute("SELECT npc_id, status FROM v_dip_attendance WHERE meeting_id = ? AND conv_id = ?",
                            (m["meeting_id"], conv)).fetchall():
            commit = C.latest_open(db, a["npc_id"], meeting_id=m["meeting_id"])
            if commit is None:
                continue
            if a["status"] == "present":
                C.set_status(db, timeline, commit, "kept", "came to the meeting", gt)
            else:
                C.set_status(db, timeline, commit, "broken", "did not come to the meeting", gt)
        out.append(m["meeting_id"])
    return out


def meeting_of_conversation(db, conv_id: int) -> int | None:
    row = db.execute("SELECT meeting_id FROM dip_meeting_change WHERE conv_id = ? AND status = 'convened'"
                     " ORDER BY change_id DESC LIMIT 1", (conv_id,)).fetchone()
    return row[0] if row else None


def unresolved(db, meeting_id: int) -> list[dict]:
    """Issues raised at a meeting (proposals, conditions, disagreements) with no later agreement on the same issue."""
    raised = db.execute("SELECT * FROM v_dip_statement WHERE meeting_id = ? AND kind IN ('proposal','condition',"
                        "'disagreement','request') ORDER BY game_time, stmt_id", (meeting_id,)).fetchall()
    agreed = {r[0] for r in db.execute("SELECT DISTINCT issue FROM v_dip_statement WHERE meeting_id = ?"
                                       " AND kind = 'agreement'", (meeting_id,))}
    return [dict(r) for r in raised if r["issue"] not in agreed]
