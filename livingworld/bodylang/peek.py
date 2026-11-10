"""A woman's eyes drifting down when the player is naked and exposed, from her mood and her view of him.

Part of body language: the server works out how likely each adult woman is to glance (``peek``, 0..1) and
sends it as the sixth field of her expression profile; the game does the glancing on its own (Mantella-Spell
``LwBodyPeek``: a look at the player's groin for a second or two, now and then, while he is naked with The
New Gentleman's bone on him and she is within a few metres). No LLM, no extra call.

What raises it: attraction to him, liking him, knowing him, a good mood, a bold nature. What lowers it: fear
of him, disliking him, irritation, a reserved or anxious nature. A glance is never consent to anything and
changes nothing about what she is willing to do (the same rule as mood and drink).

The game sends a ``presence`` record (``what=peek``) when she glances, so her next line may know she looked;
it changes none of her feelings (body-language loop guard: her own reaction never feeds itself).

Men and children never get a peek value. ``MANTELLA_LW_PEEK=0`` turns it off (the start reply carries
``mantella_lw_peek_on``); body language off turns it off too.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_PEEK"
KEY_PEEK_ON = "mantella_lw_peek_on"


def peek_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _t(traits: dict[str, float], name: str) -> float:
    return traits.get(name, 0.5)


def propensity(traits: dict[str, float], affinity: float, trust: float, fear: float, familiarity: float,
               attraction: float, feelings, mood_score: int | None) -> float:
    """0 never looks, 1 can hardly help it. Most people land between 0.1 and 0.5."""
    t = lambda name: _t(traits, name)
    value = 0.22
    value += 0.55 * attraction + 0.2 * max(affinity, 0.0) + 0.1 * familiarity + 0.05 * max(trust, 0.0)
    value -= 0.35 * max(-affinity, 0.0) + 0.5 * fear
    value += 0.2 * (t("bold") - 0.5) + 0.1 * (t("warmth") - 0.5)
    value -= 0.25 * (t("reserve") - 0.5) + 0.15 * (t("anxiety") - 0.5)
    value -= 0.4 * feelings.get("fear") + 0.25 * feelings.get("irritation") + 0.1 * feelings.get("unease")
    if mood_score is not None:
        value += 0.03 * (mood_score - 5)
    return round(max(0.0, min(0.9, value)), 2)


def for_npc(db, npc_id: int, player_id: int | None, traits: dict[str, float], feelings, now: float) -> float:
    from livingworld import mood
    from livingworld.perception.attire import sex_of
    from livingworld.store.minors import is_child
    if player_id is None or sex_of(db, npc_id) != "female" or is_child(db, npc_id):
        return 0.0
    row = db.execute("SELECT affinity, trust, fear, familiarity, attraction FROM v_relationship"
                     " WHERE from_id = ? AND to_id = ?", (npc_id, player_id)).fetchone()
    affinity, trust, fear, familiarity, attraction = (float(v or 0.0) for v in row) if row else (0.0,) * 5
    carried = mood.carried_over(db, npc_id, now)
    return propensity(traits, affinity, trust, fear, familiarity, attraction, feelings,
                      carried.score if carried else None)
