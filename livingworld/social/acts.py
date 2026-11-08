"""What each kind of claim means: how it is said, how interesting it is, and how hearing it changes a
listener's view of the person it is about. Numbers are starting points, tuned by playing."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Act:
    verb: str                     # "grabbed": "<subject> grabbed <object><detail>"
    interest: float               # 0..1, how worth repeating it is
    deltas: dict[str, float] = field(default_factory=dict)   # listener -> subject, at magnitude 1, fully believed
    harms_object: bool = False    # bad for the object: listeners close to the object take it harder


ACTS: dict[str, Act] = {
    "touched": Act("touched", 0.25, {"suspicion": 0.08}, harms_object=True),
    "grabbed": Act("grabbed", 0.5, {"trust": -0.15, "suspicion": 0.2}, harms_object=True),
    "held": Act("grabbed and held", 0.7, {"trust": -0.25, "suspicion": 0.3, "fear": 0.06}, harms_object=True),
    "drank": Act("was drinking", 0.15),
    "bought_round": Act("bought a round of drinks for", 0.3, {"affinity": 0.05, "respect": 0.02}),
    "helped": Act("helped", 0.35, {"affinity": 0.04, "respect": 0.04, "trust": 0.03}),        # work/ tasks
    "let_down": Act("let down", 0.45, {"trust": -0.06, "respect": -0.04}, harms_object=True),  # work/ tasks
    # romance (social/romance.py); a partner's own reaction is in romance.on_learned
    "flirted": Act("flirted with", 0.45),
    "spent_night": Act("spent the night with", 0.8),
    "caught_with": Act("was caught in bed with", 0.9),
    "is_courting": Act("is courting", 0.5),
    "is_seeing": Act("is seeing", 0.45),
    "together": Act("is now with", 0.45),
    "betrothed": Act("is betrothed to", 0.6),
    "married": Act("married", 0.7),
    "broke_up": Act("broke up with", 0.6),
    "reconciled": Act("got back together with", 0.5),
    "reported": Act("", 0.3),     # said in conversation; meaning comes from the claim's valence
}

SECONDHAND = 0.6        # hearing about something moves an opinion less than seeing it
CLOSE_TO_OBJECT = 1.5   # ...unless it was done to someone you are close to


def act(name: str) -> Act:
    return ACTS.get(name, ACTS["reported"])


def listener_deltas(act_name: str, valence: float, magnitude: float, believed: float,
                    close_to_object: bool) -> dict[str, float]:
    """How a listener's view of the claim's subject changes when they hear it."""
    a = act(act_name)
    base = dict(a.deltas)
    if act_name == "reported" or not base:
        if valence > 0:
            base = {"affinity": 0.08 * valence, "trust": 0.05 * valence, "admiration": 0.06 * valence}
        elif valence < 0:
            base = {"affinity": 0.08 * valence, "trust": 0.06 * valence, "suspicion": -0.06 * valence}
    scale = (0.5 + magnitude) * believed * SECONDHAND
    if close_to_object and (a.harms_object or valence < 0):
        scale *= CLOSE_TO_OBJECT
    return {k: round(v * scale, 4) for k, v in base.items() if abs(v * scale) >= 0.001}
