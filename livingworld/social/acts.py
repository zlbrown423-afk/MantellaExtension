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
    # reputation (S2): how it changes the holder's view of the subject in each domain, at magnitude 1, seen
    # first-hand and fully believed. Positive is the good end of each domain (see social/reputation.py).
    domains: dict[str, float] = field(default_factory=dict)


ACTS: dict[str, Act] = {
    "touched": Act("touched", 0.25, {"suspicion": 0.08}, harms_object=True, domains={"decency": -0.08}),
    "grabbed": Act("grabbed", 0.5, {"trust": -0.15, "suspicion": 0.2}, harms_object=True,
                   domains={"gentleness": -0.12, "decency": -0.08}),
    "held": Act("grabbed and held", 0.7, {"trust": -0.25, "suspicion": 0.3, "fear": 0.06}, harms_object=True,
                domains={"gentleness": -0.22, "decency": -0.12}),
    "drank": Act("was drinking", 0.15),
    "bought_round": Act("bought a round of drinks for", 0.3, {"affinity": 0.05, "respect": 0.02},
                        domains={"generosity": 0.18}),
    "helped": Act("helped", 0.35, {"affinity": 0.04, "respect": 0.04, "trust": 0.03},
                  domains={"reliability": 0.15, "generosity": 0.08}),        # work/ tasks
    "let_down": Act("let down", 0.45, {"trust": -0.06, "respect": -0.04}, harms_object=True,
                    domains={"reliability": -0.25}),  # work/ tasks
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
    # guard careers (livingworld/guard): "<member> was taken on by <leader> as a Recruit in the Whiterun Guard"
    "enlisted": Act("was taken on by", 0.45, {"respect": 0.03}, domains={"standing": 0.15, "lawfulness": 0.05}),
    "promoted": Act("was promoted by", 0.4, {"respect": 0.04}, domains={"standing": 0.2}),
    "demoted": Act("was demoted by", 0.45, {"respect": -0.04}, domains={"standing": -0.2}),
    "dismissed": Act("was dismissed by", 0.6, {"respect": -0.06, "trust": -0.03},
                     domains={"standing": -0.3, "reliability": -0.1}),
    "reinstated": Act("was taken back by", 0.35),
    "commended": Act("was commended by", 0.35, {"respect": 0.04, "trust": 0.02}),
    "reprimanded": Act("was reprimanded by", 0.4, {"respect": -0.03}),
    "arrested": Act("arrested", 0.45, {"respect": 0.03}, domains={"standing": 0.1, "lawfulness": 0.1}),
    "cleared": Act("drove off", 0.55, {"respect": 0.05}, domains={"standing": 0.15, "lawfulness": 0.1}),
    # reputation (S2): things the game reports the player doing (perception records gift, theft, hit)
    "gifted": Act("gave a gift to", 0.35, {"affinity": 0.05, "trust": 0.02}, domains={"generosity": 0.25}),
    "stole": Act("stole from", 0.7, {"trust": -0.25, "suspicion": 0.3}, harms_object=True,
                 domains={"honesty": -0.4, "lawfulness": -0.35}),
    "pickpocketed": Act("picked the pocket of", 0.8, {"trust": -0.3, "suspicion": 0.35}, harms_object=True,
                        domains={"honesty": -0.5, "lawfulness": -0.35}),
    "hit": Act("hit", 0.75, {"trust": -0.2, "fear": 0.12, "affinity": -0.1}, harms_object=True,
               domains={"gentleness": -0.4, "lawfulness": -0.15}),
    "reported": Act("", 0.3),     # said in conversation; meaning comes from the claim's valence
}

# the verb when nobody is named as having done it ("Torvar was promoted to Sergeant in the Whiterun Guard")
NO_OBJECT = {"stole": "stole", "gifted": "gave a gift", "enlisted": "joined", "promoted": "was promoted", "demoted": "was demoted", "dismissed": "was dismissed",
             "reinstated": "was taken back",
             "commended": "was commended", "reprimanded": "was reprimanded",
             "arrested": "arrested", "cleared": "drove off"}

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
