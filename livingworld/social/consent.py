"""Consent: one person's own answer to one invitation, at one phase.

``decide`` is the only way an encounter gets a yes. It is asked separately for each person, at every
phase (invite, arrival, start, and later during a scene), and every answer is written to
``consent_decision`` with the factors that produced it.

What it reads: the person's own traits, their own side of the relationship (``v_relationship`` from
them to the other), the agreed bond stage, their mood, how much they have had to drink, and the place.

What it never reads, on purpose: gifts, gold, debts or favours (``obligation``), rank or employer, the
other person's wish, how many times they have been asked, and anything the player says or orders.
Attraction feeds desire only; it never outweighs a hard limit. A marriage or other bond only changes
which kinds of encounter are allowed at all; spouses are asked like anyone else.
Drink never makes a yes more likely; past "drunk" it rules intimacy out (no clear yes is possible).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from livingworld.perception import intoxication
from livingworld.store.minors import is_child

PHASES = ("invite", "arrival", "start", "during")
INTIMATE_KINDS = {"intimate"}

# how far a bond has come, for intimacy_boundary
STAGE_RANK = {None: 0, "ended": 0, "courting": 1, "dating": 2, "affair": 2, "partners": 3, "betrothed": 3, "married": 4}
BOUNDARY_NEEDS = {"never": 99, "married_only": 4, "committed_only": 3, "after_courtship": 2, "casual_ok": 1}

FEAR_LIMIT = 0.3
LOW_MOOD = 2           # mood meter 0..10: at or below this, nobody wants company of that kind
ANGRY_WORDS = ("angry", "furious", "enraged", "livid", "hostile")
DRUNK_STAGES = {"drunk", "very drunk"}
GRIEF_DAYS = 7.0       # a death close to them this recently rules out romance


@dataclass
class Decision:
    answer: str                      # 'yes' | 'no' | 'withdrew'
    reasons: dict = field(default_factory=dict)

    @property
    def yes(self) -> bool:
        return self.answer == "yes"


def _rel(db, a: int, b: int) -> dict:
    row = db.execute("SELECT trust, affinity, fear, attraction, familiarity, intimacy, satisfaction"
                     " FROM v_relationship WHERE from_id = ? AND to_id = ?", (a, b)).fetchone()
    keys = ("trust", "affinity", "fear", "attraction", "familiarity", "intimacy", "satisfaction")
    return dict(zip(keys, row)) if row else dict.fromkeys(keys, 0.0)


def _mood(db, npc: int, now_gt: float) -> tuple[int | None, str]:
    row = db.execute("SELECT mood_json, updated_gt FROM emotion_state WHERE npc_id = ?", (npc,)).fetchone()
    if not row or row[1] > now_gt + 1e-9 or now_gt - row[1] > 1.0:
        return None, ""
    try:
        data = json.loads(row[0])
        return int(data.get("score")), str(data.get("label") or "")
    except (ValueError, TypeError):
        return None, ""


def _grieving(db, npc: int, now_gt: float) -> bool:
    return db.execute(
        "SELECT 1 FROM v_visible_event e JOIN v_relationship r ON r.from_id = ? AND r.to_id IN"
        " (SELECT value FROM json_each(e.actors_json))"
        " WHERE e.type = 'death' AND e.game_time BETWEEN ? AND ? AND (r.kinship IS NOT NULL OR r.affinity >= 0.5)"
        " LIMIT 1", (npc, now_gt - GRIEF_DAYS, now_gt)).fetchone() is not None


def decide(db, npc: int, other: int, kind: str, phase: str, traits: dict, stage: str | None, now_gt: float,
           place_privacy: float | None = None, encounter_id: int | None = None, timeline: int | None = None,
           jitter: float = 0.0, log: bool = True) -> Decision:
    """npc's own answer to time of ``kind`` with ``other``. ``traits`` are npc's romance traits.
    ``jitter`` (-0.05..0.05, from the caller's seeded dice) stands for the day-to-day mood of the moment."""
    reasons: dict = {"kind": kind, "phase": phase, "stage": stage}
    no = None
    rel = _rel(db, npc, other)
    reasons["rel"] = {k: round(v, 3) for k, v in rel.items()}

    # ---- hard limits ----
    if is_child(db, npc) or is_child(db, other):
        no = "child"
    elif db.execute("SELECT 1 FROM npc WHERE npc_id IN (?, ?) AND is_player = 1", (npc, other)).fetchone():
        no = "player_not_simulated"
    elif rel["fear"] > FEAR_LIMIT:
        no = "afraid_of_them"
    score, label = _mood(db, npc, now_gt)
    reasons["mood"] = score
    if no is None and score is not None and score <= LOW_MOOD:
        no = "mood_too_low"
    if no is None and any(w in label.lower() for w in ANGRY_WORDS):
        no = "angry"
    if no is None and _grieving(db, npc, now_gt):
        no = "grieving"
    if no is None and kind in INTIMATE_KINDS:
        need = BOUNDARY_NEEDS.get(traits.get("intimacy_boundary", "committed_only"), 3)
        reasons["boundary"] = traits.get("intimacy_boundary")
        if STAGE_RANK.get(stage, 0) < need:
            no = "boundary"
        else:
            stage_of_drink = intoxication.drink_state(db, npc, now_gt).stage
            reasons["drink_stage"] = stage_of_drink     # logged for audit; never adds to the wish below
            if stage_of_drink in DRUNK_STAGES:
                no = "too_drunk_to_consent"
        if no is None and place_privacy is not None and place_privacy < 0.7:
            no = "not_private"

    # ---- their own wish, against their own threshold ----
    if no is None:
        trust = max(0.0, rel["trust"])
        comfort = 0.4 * trust + 0.3 * rel["familiarity"] + 0.3 * (rel["satisfaction"] + 1) / 2
        mood = 0.0 if score is None else (score - 5) * 0.02
        if kind in INTIMATE_KINDS:
            desire = 0.4 * traits["drive"] + 0.35 * rel["attraction"] + 0.25 * rel["intimacy"]
            fit = (place_privacy if place_privacy is not None else 0.8) - 0.3 * traits["modesty"]
            wish = 0.5 * desire + 0.35 * comfort + 0.15 * fit + mood + jitter
            threshold = 0.42 + 0.08 * traits["shy"]
        elif kind == "flirt":
            desire = 0.5 * traits["flirtatious"] + 0.5 * rel["attraction"]
            wish = 0.6 * desire + 0.4 * comfort + mood + jitter
            threshold = 0.35 + 0.1 * traits["shy"]
        else:   # date, affection
            desire = 0.5 * traits["romantic"] + 0.5 * rel["attraction"]
            wish = 0.5 * desire + 0.5 * comfort + mood + jitter
            threshold = 0.38 + 0.08 * traits["shy"]
        reasons["wish"], reasons["threshold"] = round(wish, 3), round(threshold, 3)
        if wish < threshold:
            no = "does_not_want_to"

    answer = "yes" if no is None else ("withdrew" if phase == "during" else "no")
    if no:
        reasons["why"] = no
    if log:
        db.execute("INSERT INTO consent_decision (timeline_id, encounter_id, npc_id, other_id, kind, phase, answer,"
                   " reasons_json, game_time) VALUES (COALESCE(?, (SELECT timeline_id FROM timeline WHERE is_active = 1)),"
                   " ?,?,?,?,?,?,?,?)",
                   (timeline, encounter_id, npc, other, kind, phase, answer, json.dumps(reasons), now_gt))
    return Decision(answer, reasons)
