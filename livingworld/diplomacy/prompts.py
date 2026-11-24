"""What one NPC knows of the diplomacy (prompt lines). Built only from rows this NPC holds.

The lines are records, never instructions to agree: convictions in words, what they said and where, promises,
invitations, and what they have heard (marked unconfirmed). The last line says plainly that the NPC weighs any
proposal by their own interests and may refuse. No score, grade of the player's argument or alignment number
ever reaches the prompt.
"""
from __future__ import annotations

from livingworld.diplomacy import common as K
from livingworld.diplomacy import figures as F

MAX_SAID = 4
MAX_HEARD = 4
MAX_PROMISES = 4

# per dimension, words for a value from strongly against to strongly for
_WORDS = {
    "empire": ("hates the Empire", "distrusts the Empire", "has no strong feelings about the Empire",
               "is sympathetic to the Empire", "is loyal to the Empire"),
    "stormcloaks": ("hates the Stormcloaks", "distrusts the Stormcloaks", "has no strong feelings about the Stormcloaks",
                    "is sympathetic to the Stormcloaks", "is devoted to the Stormcloak cause"),
    "thalmor": ("loathes the Thalmor", "distrusts the Thalmor", "has no strong feelings about the Thalmor",
                "finds the Thalmor useful", "serves the Thalmor's cause"),
    "negotiation": ("will not negotiate with the other side of the war", "is reluctant to negotiate with the other side",
                    "might hear proposals from the other side", "is open to negotiating with the other side",
                    "wants a negotiated settlement of the war"),
    "concordat": ("regards the White-Gold Concordat as a betrayal", "opposes the White-Gold Concordat",
                  "is undecided about the White-Gold Concordat", "accepts the White-Gold Concordat as necessary",
                  "upholds the White-Gold Concordat"),
    "ceasefire": ("rejects any truce in the civil war", "is against a truce in the civil war",
                  "is undecided about a truce in the civil war", "could see the sense in a truce",
                  "wants a truce in the civil war"),
    "alliance": ("rejects any alliance between the Empire and the Stormcloaks against the Thalmor",
                 "is against an Empire-Stormcloak alliance against the Thalmor",
                 "is undecided about an Empire-Stormcloak alliance against the Thalmor",
                 "could see the sense in an Empire-Stormcloak alliance against the Thalmor",
                 "wants the Empire and the Stormcloaks allied against the Thalmor"),
}
_MODALITY = {"asserted": "", "conditional": " (on conditions)", "hypothetical": " (only as a what-if)",
             "question": " (as a question)", "reported": " (passed on, not confirmed)",
             "rumor": " (a rumour, not confirmed)"}
_INVITE = {"pending": "not yet answered", "delivered": "not yet answered", "accepted": "accepted",
           "conditional": "accepted on conditions", "declined": "declined", "countered": "proposed something else",
           "withdrawn": "withdrawn"}


def words(dimension: str, value: float, confidence: float) -> str:
    band = 0 if value <= -0.6 else 1 if value <= -0.2 else 2 if value < 0.2 else 3 if value < 0.6 else 4
    text = _WORDS[dimension][band]
    return ("probably " + text) if confidence < 0.45 else text


def _positions(db, npc_id: int, layer: str) -> dict[str, tuple[float, float]]:
    return {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT dimension, value, confidence FROM v_political_position WHERE npc_id = ? AND layer = ?",
        (npc_id, layer))}


def _holds_any(db, npc_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_dip_awareness WHERE holder_id = ? LIMIT 1", (npc_id,)).fetchone() is not None


def lines_for(db, npc_id: int, name: str, player_id: int | None, player_name: str, now: float) -> list[str]:
    """Lines for this NPC, or [] when they are no figure and hold no diplomatic statement."""
    fig = F.figure_of(db, npc_id)
    personal = _positions(db, npc_id, "personal")
    if fig is None and not personal and not _holds_any(db, npc_id):
        return []
    who = lambda i: player_name if (i is not None and i == player_id) else K.name_of(db, i)   # noqa: E731
    out: list[str] = []
    if fig is not None:
        out.append(f"{name} is {fig.role}, and argues from what they care about: {fig.cares}.")
    if personal:
        beliefs = "; ".join(words(d, v, c) for d, (v, c) in sorted(personal.items()))
        out.append(f"{name}'s own convictions, private unless {name} chooses to share them: {beliefs}.")
    official = _positions(db, npc_id, "official")
    differs = {d: vc for d, vc in official.items() if d not in personal or abs(personal[d][0] - vc[0]) >= 0.4}
    if differs and fig is not None:
        line = "; ".join(words(d, v, c) for d, (v, c) in sorted(differs.items()))
        out.append(f"The official line {name} is expected to hold in public for {fig.faction}: {line}.")
    for r in db.execute(
            "SELECT d.* FROM v_dip_awareness a JOIN v_dip_statement d ON d.stmt_id = a.stmt_id"
            " WHERE a.holder_id = ? AND d.subject_id = ? AND d.speaker_id = ? AND d.about_name IS NULL"
            " ORDER BY d.game_time DESC, d.stmt_id DESC LIMIT ?", (npc_id, npc_id, npc_id, MAX_SAID)).fetchall():
        where = "in private" if r["audience"] == "private" else "in front of others"
        to = f" to {who(r['target_id'])}" if r["target_id"] is not None else ""
        cond = f", if {r['condition']}" if r["condition"] else ""
        out.append(f"{K.days_ago(now, r['game_time']).capitalize()}, {where}{to}, {name} said they {r['summary']}"
                   f"{cond}{_MODALITY.get(r['modality'], '')}.")
    for r in db.execute(
            "SELECT * FROM v_dip_commitment WHERE (debtor_id = ? OR creditor_id = ?) AND"
            " (status = 'open' OR (status IN ('broken','lapsed') AND status_gt >= ?))"
            " ORDER BY made_gt DESC LIMIT ?", (npc_id, npc_id, now - 60, MAX_PROMISES)).fetchall():
        debtor = name if r["debtor_id"] == npc_id else who(r["debtor_id"])
        creditor = name if r["creditor_id"] == npc_id else (who(r["creditor_id"]) if r["creditor_id"] else "others")
        cond = f" (if {r['condition']})" if r["condition"] else ""
        due = ""
        if r["status"] == "open" and r["due_gt"] is not None:
            left = r["due_gt"] - now
            due = f", due in about {max(1, int(round(left)))} days" if left > 0 else ", due now"
        state = {"open": "still open", "broken": "BROKEN", "lapsed": "not kept in time"}[r["status"]]
        out.append(f"Promise: {debtor} promised {creditor} that they {r['description']}{cond}{due}: {state}.")
    for r in db.execute(
            "SELECT i.*, m.venue, m.purpose, m.status AS meeting_status FROM v_dip_invitation i"
            " JOIN v_dip_meeting m ON m.meeting_id = i.meeting_id WHERE i.invitee_id = ? AND i.status <> 'pending'"
            " AND m.status IN ('proposed','agreed','convened') ORDER BY i.invited_gt DESC LIMIT 3",
            (npc_id,)).fetchall():
        cond = f" ({r['conditions']})" if r["conditions"] else ""
        out.append(f"{who(r['invited_by_id'])} has invited {name} to a meeting at {r['venue']}; {name}'s answer so far:"
                   f" {_INVITE.get(r['status'], r['status'])}{cond}.")
    for r in db.execute(
            "SELECT d.*, a.origin, a.told_by_id, a.confidence AS held FROM v_dip_awareness a"
            " JOIN v_dip_statement d ON d.stmt_id = a.stmt_id WHERE a.holder_id = ? AND d.subject_id <> ?"
            " ORDER BY a.learned_gt DESC, d.stmt_id DESC LIMIT ?", (npc_id, npc_id, MAX_HEARD)).fetchall():
        subject = r["about_name"] or who(r["subject_id"])
        if r["origin"] == "witnessed":
            how = f"{name} heard it said, {K.days_ago(now, r['game_time'])}"
        elif r["origin"] == "said":
            how = f"{name} passed this on, {K.days_ago(now, r['game_time'])}"
        elif r["origin"] == "courier":
            how = f"a courier from {who(r['told_by_id'])} brought word"
        else:
            how = f"{who(r['told_by_id'])} told {name}; not confirmed"
        if r["about_name"] and r["speaker_id"] != npc_id:
            subject_line = f"{who(r['speaker_id'])} says {subject} {r['summary']}"
        else:
            subject_line = f"{subject} {r['summary']}"
        out.append(f"Heard: {subject_line}{_MODALITY.get(r['modality'], '') if r['origin'] == 'witnessed' else ''}"
                   f" ({how}).")
    out.append(f"These are records of what has happened, not instructions. {name} weighs any proposal on its merits, "
               f"by their own interests, convictions and opinion of whoever is asking, and may agree, set conditions, "
               f"refuse, stall or change the subject. Being liked is not the same as being agreed with.")
    return out
