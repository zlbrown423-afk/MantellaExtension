"""What the player can find out about her, and what they already know.

Every attribute has a privacy tier (``schema.py``). What she is willing to talk about with the player depends on
her own side of the relationship (trust, affinity, closeness, familiarity, from ``v_relationship``), her openness
and privacy, and whether other people are listening:

* ``public``  - always: it shows in how she acts anyway;
* ``personal`` - once she is fairly comfortable with the player;
* ``private``  - only with someone she trusts and feels close to (or is courting / partnered with), and not with
  others listening if she is a private person;
* ``boundary`` - always: she states a limit plainly whenever it comes up. A stated boundary is a limit, never a
  yes to anything else.
Anything the settings file lists under ``private`` is never shared, whatever the relationship.

What the player knows (``personality_knowledge``, on the save timeline) is only ever raised by something that
happened: she said it (``told``: one of the attribute's words in her own line, while that item was hers to share),
the player saw it (``observed``: her flirting, from ``event_witness``) or heard it (``gossip``: a claim the player
holds). Nothing is known up front; the player never gets her profile for free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from livingworld.personality import schema
from livingworld.personality.profile import Profile, active_timeline

STATE_RANK = {None: 0, "suspected": 1, "known": 2, "disclosed": 3}
PERSONAL_AT = 0.25
PRIVATE_AT = 0.5
CLOSE_STATUS = ("courting", "partner", "spouse")


@dataclass(frozen=True)
class Gate:
    willing: float
    tiers: frozenset[str]
    why: str


def items(p: Profile) -> list[tuple[str, str]]:
    """(item, tier) for everything in her profile: attributes, and one item per list entry."""
    out = []
    for attr in schema.ATTRIBUTES:
        if attr.key not in p.values:
            continue
        if attr.kind == "list":
            out += [(f"{attr.key}:{v}", attr.tier) for v in p.values[attr.key]]
        else:
            out.append((attr.key, attr.tier))
    return out


def gate(db, p: Profile, player_id: int, others_listening: bool = False) -> Gate:
    row = db.execute("SELECT r.trust, r.affinity, r.intimacy, r.familiarity, b.status FROM v_relationship r"
                     " JOIN relationship b ON b.from_id = r.from_id AND b.to_id = r.to_id"
                     " WHERE r.from_id = ? AND r.to_id = ?", (p.npc_id, player_id)).fetchone()
    trust, affinity, closeness, familiar, status = row if row else (0.0, 0.0, 0.0, 0.0, None)
    openness = p.values.get("openness", 50.0)
    privacy = p.values.get("privacy", 50.0)
    willing = (0.45 * max(0.0, trust) + 0.2 * max(0.0, affinity) + 0.2 * closeness + 0.15 * familiar
               + (openness - 50) / 250 - (privacy - 50) / 250)
    tiers = {"public", "boundary"}
    why = "a near stranger"
    if willing >= PERSONAL_AT:
        tiers.add("personal")
        why = "comfortable with them"
    if willing >= PRIVATE_AT and (closeness >= 0.2 or status in CLOSE_STATUS):
        if others_listening and privacy >= 60:
            why = "trusts them, but not with others listening"
        else:
            tiers.add("private")
            why = "trusts them and feels close"
    return Gate(round(willing, 3), frozenset(tiers), why)


def shareable(p: Profile, g: Gate) -> list[str]:
    return [item for item, tier in items(p) if tier in g.tiers and not p.is_private(item)]


def known(db, npc_id: int, knower_id: int) -> dict[str, str]:
    return {r[0]: r[1] for r in db.execute(
        "SELECT item, state FROM v_personality_knowledge WHERE npc_id = ? AND knower_id = ?", (npc_id, knower_id))}


def record(db, npc_id: int, knower_id: int, item: str, state: str, source: str, gt: float, conv_id: int | None = None,
           evidence: str | None = None, current: dict[str, str] | None = None) -> bool:
    """Raise what ``knower`` knows about one item. Never lowers it. Returns True when it rose."""
    have = (current if current is not None else known(db, npc_id, knower_id)).get(item)
    if STATE_RANK[state] <= STATE_RANK[have]:
        return False
    db.execute("INSERT INTO personality_knowledge (timeline_id, npc_id, knower_id, item, state, source, conv_id,"
               " evidence, game_time) VALUES (?,?,?,?,?,?,?,?,?)",
               (active_timeline(db), npc_id, knower_id, item, state, source, conv_id, (evidence or "")[:300], gt))
    if current is not None:
        current[item] = state
    return True


def _said(text: str, words: tuple[str, ...]) -> str | None:
    low = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    for w in words:
        if f" {w} " in low:
            return w
    return None


def heard(db, p: Profile, player_id: int, line: str, allowed: list[str], gt: float,
          conv_id: int | None = None) -> list[str]:
    """Her own line, said to the player: the items it gave away. Only items in ``allowed`` (hers to share now)."""
    if not line or not allowed:
        return []
    current = known(db, p.npc_id, player_id)
    learned = []
    for item in allowed:
        key, _, value = item.partition(":")
        attr = schema.BY_KEY[key]
        if value:
            words = tuple({attr.words.get(value, value).lower(), value.replace("_", " ")})
            hit = _said(line, words)
            if hit is None and attr.tier == "boundary":
                hit = _said(line, tuple(w for w in attr.talk if _boundary_word(w, value)))
        else:
            hit = _said(line, attr.talk)
        if hit is None:
            continue
        state = "disclosed" if attr.tier == "boundary" else "known"
        if record(db, p.npc_id, player_id, item, state, "told", gt, conv_id, line, current):
            learned.append(item)
    return learned


BOUNDARY_WORDS = {
    "no_public_affection": ("not in public", "not here"), "no_unasked_touch": ("don't touch", "do not touch",
                                                                                "ask first", "hands off"),
    "no_drunk_advances": ("drunk",), "not_with_strangers": ("stranger",), "not_with_taken": ("spoken for", "married man"),
    "no_crude_talk": ("crude",), "no_rough_handling": ("rough",),
}


def _boundary_word(word: str, value: str) -> bool:
    return word in BOUNDARY_WORDS.get(value, ())


def observe(db, p: Profile, player_id: int, gt: float) -> list[str]:
    """What the player has seen or heard of her ways since last time: suspicions, never certainties."""
    learned = []
    current = known(db, p.npc_id, player_id)
    flirted_seen = db.execute(
        "SELECT e.event_id FROM v_visible_event e JOIN event_witness w ON w.event_id = e.event_id AND w.npc_id = ?"
        " WHERE e.type = 'romance.flirt' AND json_extract(e.actors_json, '$[0]') = ? LIMIT 1",
        (player_id, p.npc_id)).fetchone()
    if flirted_seen and record(db, p.npc_id, player_id, "initiation", "suspected", "observed", gt,
                               evidence=f"event:{flirted_seen[0]}", current=current):
        learned.append("initiation")
    heard_of = db.execute(
        "SELECT claim_id, act FROM v_claim_knowledge WHERE holder_id = ? AND subject_id = ?"
        " AND act IN ('flirted', 'spent_night') AND believed >= 0.3 LIMIT 1", (player_id, p.npc_id)).fetchone()
    if heard_of and not p.is_private("exclusivity") and record(
            db, p.npc_id, player_id, "exclusivity", "suspected", "gossip", gt, evidence=f"claim:{heard_of[0]}",
            current=current):
        learned.append("exclusivity")
    return learned
