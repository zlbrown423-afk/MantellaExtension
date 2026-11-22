"""What an official in the conversation knows and thinks about the Plains Wardens' charter (prompt lines).

Rules decided all of it (``reckoning.py``); these lines only let the LLM voice it. In L1 the charter cannot be
renegotiated in conversation, so officials are told not to agree new terms (L2 adds negotiation by words).
"""
from __future__ import annotations

import json

from livingworld.lordship import COMPANY
from livingworld.lordship import charter as C
from livingworld.lordship import data as D
from livingworld.lordship import reckoning as R

POSITION_WORDS = {
    "champion": "a firm champion of the Plains Wardens",
    "support": "broadly supportive of the Plains Wardens",
    "wary": "wary of the Plains Wardens",
    "oppose": "opposed to how the Plains Wardens are going",
    "hostile": "hostile to the Plains Wardens",
}
STATUS_WORDS = {
    "active": "in force",
    "under_review": "under review by the Jarl",
    "suspended": "suspended by the Jarl: no stipend is paid and the Wardens hold no authority until it is restored",
}
POWER_WORDS = {
    "patrol": "patrol the district's roads", "detain": "detain suspects for the guard", "build": "build posts",
    "appoint_representatives": "appoint settlement representatives", "road_fees": "levy road fees",
    "minor_hearings": "hear minor disputes", "recruit_locally": "recruit locally", "own_treasury": "keep their own treasury",
}


def charter_summary(charter: C.Charter, player_name: str, now: float) -> str:
    t = charter.terms
    district = D.district(charter.district_key)
    where = district.name if district else "a rural district"
    powers = ", ".join(POWER_WORDS.get(p, p) for p in t.get("powers", [])) or "no special powers"
    review = ""
    if charter.review_gt is not None:
        days = int(round(charter.review_gt - now))
        review = (f" The charter is due for the Jarl's review in about {days} days." if days > 0
                  else " The charter is due for the Jarl's review now.")
    return (f"{player_name} founded the Plains Wardens, a mounted company, and holds Jarl Balgruuf's charter for it "
            f"({charter.title}, version {charter.version}), which is {STATUS_WORDS.get(charter.status, charter.status)}. "
            f"Territory: {where}; the Jarl keeps the city, Dragonsreach and all civil government. Powers: {powers}. "
            f"Terms: {int(t.get('stipend_per_year') or 0)} gold a year from the steward plus "
            f"{int(t.get('equipment_per_year') or 0)} for equipment; at most {int(t.get('personnel_cap') or 0)} riders; "
            f"{int(t.get('patrols_per_week') or 0)} patrols a week; all riders at the Whiterun gate within "
            f"{int(t.get('mobilise_within_days') or 1)} day(s) of a call; a written report to the steward every "
            f"{int(t.get('report_every_days') or 30)} days.{review}")


def _last_month(db, charter: C.Charter) -> str | None:
    period = R.periods_done(db, charter)
    if period <= 0:
        return None
    rows = db.execute("SELECT term_key, outcome, note FROM v_compliance WHERE charter_id = ? AND period = ?"
                      " AND outcome <> 'excused'", (charter.charter_id, period)).fetchall()
    if not rows:
        return None
    return "Last month's reckoning: " + "; ".join(f"{note} ({outcome})" for _, outcome, note in rows) + "."


def official_lines(db, npc_id: int, name: str, player_id: int | None, player_name: str, now: float) -> list[str]:
    """Lines for this NPC if they are a court official with a view on the charter; [] otherwise."""
    official = C.official_of(db, npc_id)
    if official is None or player_id is None:
        return []
    charter = C.current(db)
    if charter is None:
        return []
    lines = [charter_summary(charter, player_name, now)]
    month = _last_month(db, charter)
    if month:
        lines.append(month)
    row = db.execute("SELECT position, reasons_json FROM v_official_stance_now WHERE npc_id = ? AND subject = ?",
                     (npc_id, COMPANY)).fetchone()
    if row is not None:
        position, reasons = row[0], json.loads(row[1])
    else:   # met them after the last reckoning (or none yet): work it out from the latest measures
        period = max(1, R.periods_done(db, charter))
        s = R.stance(db, official, R.measure(db, charter, period, player_id), player_id)
        position, reasons = s.position, s.reasons
    because = f", because {' and '.join(reasons)}" if reasons else ""
    lines.append(f"{name}, as {official.role}, is {POSITION_WORDS[position]}{because}. {name} cares most about "
                 f"{official.cares} and argues from that, in their own manner.")
    move = db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'lordship.move' AND game_time >= ?"
                      " ORDER BY game_time DESC, event_id DESC LIMIT 1", (now - 30,)).fetchone()
    if move is not None:
        p = json.loads(move[0])
        if p.get("by") == official.key:
            lines.append(f"Recently: {p.get('text')}. {name} may raise it with {player_name}.")
        elif p.get("text"):
            lines.append(f"News at court: {p.get('text')}.")
    lines.append(f"The charter's terms can only be changed by the Jarl in a formal review: {name} may argue, warn or "
                 f"promise to speak for or against the Wardens, but agrees no new terms in this conversation.")
    return lines
