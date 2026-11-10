"""Partners who catch each other: walking in on a partner's scene, and not letting it go afterwards.

Until now a spouse or partner who saw their own partner in an intimate scene with someone else got a memory
and a drop in trust (``scenes.py``), but nothing made them say a word: in the 2026-11-10 test Hod saw Gerdur
with the player from 2.4 m and never mentioned it, not then and not in the 155 lines he spoke afterwards.

Two parts:

* **Walking in.** The game knows who is with whom from the couples list in the start reply
  (``mantella_lw_couples``, ``"Hod|Gerdur"``: spouses, and courting, dating, partners or betrothed couples;
  vanilla lovers, relationship rank 4, count too). While it follows a scene, a witness who has a clear view
  of their own partner in it, from close by, starts a confrontation (Mantella-Spell ``LwConfront``): with a
  conversation running they join it, otherwise one starts with them, the couple and the player when the
  player is near. Once a scene, at most once a minute or two. The prompt lines in ``scenes.lines_for`` tell
  the witness they walk in on it and speak up, and tell the partner in the scene who is watching.
* **Not letting it go.** For a few days after, the witness's prompt says what they saw and that it is still on
  their mind; with the partner or the other person in the conversation they bring it up.

How they react is always their own (anger, hurt, cold questions, walking out); nothing here makes anyone
violent. Forced scenes are not confrontations: those are an assault (``scenes.py``). Children are never in
any of it.

``MANTELLA_LW_CONFRONT=0`` turns both parts off (the game then never starts a confrontation).
"""
from __future__ import annotations

import json
import os

ENV = "MANTELLA_LW_CONFRONT"
KEY_COUPLES = "mantella_lw_couples"     # start reply: "A|B" for each couple, for the game's confrontations
KEY_CONFRONT_ON = "mantella_lw_confront_on"
MAX_COUPLES = 120
FRESH_DAYS = 1.0          # within a game day of seeing it: brings it up
GRIEVANCE_DAYS = 4.0      # after that, for a few days more: still on their mind


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def couples(db) -> list[str]:
    """Every couple the game should know, as ``"A|B"`` real names (adults only)."""
    from livingworld.social.romance import ACTIVE
    from livingworld.store.minors import is_child
    pairs: set[tuple[int, int]] = set()
    stages = [s for s in ACTIVE if s != "affair"]
    for a, b in db.execute(f"SELECT a_id, b_id FROM v_romance_bond WHERE stage IN ({','.join('?' * len(stages))})",
                           stages).fetchall():
        pairs.add((min(a, b), max(a, b)))
    for a, b in db.execute("SELECT from_id, to_id FROM v_relationship WHERE kinship = 'spouse'"
                           " OR status IN ('spouse', 'partner', 'courting')").fetchall():
        pairs.add((min(a, b), max(a, b)))
    names = dict(db.execute("SELECT npc_id, display_name FROM npc").fetchall())
    out = []
    for a, b in sorted(pairs):
        if a == b or a not in names or b not in names or is_child(db, a) or is_child(db, b):
            continue
        out.append(f"{names[a]}|{names[b]}")
        if len(out) >= MAX_COUPLES:
            break
    return out


def _ago(days: float) -> str:
    hours = days * 24.0
    if hours < 1.5:
        return "just a little while ago"
    if hours < 20:
        return f"about {round(hours)} hours ago"
    whole = round(days)
    return "yesterday" if whole <= 1 else f"{whole} days ago"


def grievance_lines(db, npc_id: int, name: str, now: float, present: dict[int, str]) -> list[str]:
    """The partner's scene ``name`` saw in the last few days, if any, and that it is still on their mind."""
    from livingworld.intimacy.common import partner_in
    from livingworld.intimacy.scenes import EVENT_SCENE
    rows = db.execute(
        "SELECT e.event_id, e.actors_json, e.payload_json, e.game_time FROM v_visible_event e"
        " JOIN event_witness w ON w.event_id = e.event_id"
        " WHERE e.type = ? AND w.npc_id = ? AND w.how != 'participated' AND e.game_time > ? AND e.game_time <= ?"
        " ORDER BY e.game_time DESC LIMIT 3", (EVENT_SCENE, npc_id, now - GRIEVANCE_DAYS, now + 1e-6)).fetchall()
    for _event_id, actors_json, payload_json, at in rows:
        people = [i for i in json.loads(actors_json or "[]") if i != npc_id]
        partner = partner_in(db, npc_id, people)
        if partner is None:
            continue
        payload = json.loads(payload_json or "{}")
        names = dict(db.execute(f"SELECT npc_id, display_name FROM npc WHERE npc_id IN ({','.join('?' * len(people))})",
                                people).fetchall())
        rivals = [i for i in people if i != partner]
        rival_names = " and ".join(names.get(i, "someone") for i in rivals) or "someone else"
        partner_name = names.get(partner, "their partner")
        place = f" in {payload['place_name']}" if payload.get("place_name") else ""
        line = (f"{name} saw {partner_name}, {name}'s own partner, being intimate with {rival_names}{place}"
                f" {_ago(now - at)}.")
        here = [n for i, n in present.items() if i == partner or i in rivals]
        if here and now - at <= FRESH_DAYS:
            line += (f" {name} has not let it go, and with {' and '.join(here)} here {name} brings it up: {name} wants"
                     f" answers. How is {name}'s own: anger, hurt, cold questions or saying nothing kind at all.")
        elif here:
            line += f" It still sits between them; with {' and '.join(here)} here it colours how {name} speaks."
        else:
            line += f" It is still on {name}'s mind and may come out when {name} talks about {partner_name}."
        return [line]
    return []
