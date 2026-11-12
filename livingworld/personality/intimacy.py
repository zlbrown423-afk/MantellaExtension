"""Intimacy preferences: what she enjoys, is neutral about, dislikes, or will not do, by activity group.

Groups are framework-neutral (``GROUPS``). Animation frameworks are separate adapters (``frameworks/``) that map
their own action ids onto these groups; this module never reads a framework file, and nothing here is invented
from a framework: a scene is only judged from the action ids the framework itself lists.

Each group has an interest 0..100 shown as a rating:
  strong (>= 80), interested (>= 60), neutral (40..60), dislikes (< 40), and separately **boundary** (will not do).
Groups are generated independently (one seeded draw each), so interest in one says nothing about another.

Precedence, strongest first:
1. Boundaries (generated, added over time, or set by hand) and categories switched off by hand.
2. Ratings set by hand (settings file ``"intimacy": {"ratings": {...}}``).
3. Her profile (generation reads it: comfort, desire, confidence, boundaries, who leads).
4. Change over time: after a scene she took part in, how she felt about it moves the groups that were in it, a
   little (``personality_change`` attribute ``intimacy.<group>``; at most ``DRIFT`` from the start).
5. Generated defaults.
A boundary is never removed by anything but a hand edit, and no rating is ever read as consent: consent is still
decided by ``social/consent.py`` and in conversation, case by case.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any

from livingworld.personality.profile import Profile, add_change, changes_since_reset

GROUPS: dict[str, str] = {        # key -> plain words for prompts
    "affection": "hugging and cuddling",
    "hand_holding": "holding hands",
    "kissing": "kissing",
    "caressing": "caresses and gentle touch",
    "massage": "massage and bathing together",
    "groping": "being groped",
    "manual": "intimacy with hands",
    "oral": "intimacy with mouths",
    "intercourse": "lovemaking",
    "anal": "anal intimacy",
    "toys": "toys",
    "rough": "rough play (spanking, hair pulling, choking)",
    "climax_on_body": "being finished on",
    "solo": "touching herself while watched",
    "tail": "tail play (beast races)",
    "vampire": "a vampire's bite",
}
SEXUAL = {"groping", "manual", "oral", "intercourse", "anal", "toys", "rough", "climax_on_body", "solo", "tail"}
RATINGS = ("strong", "interested", "neutral", "dislikes", "boundary")
RATING_VALUE = {"strong": 90.0, "interested": 70.0, "neutral": 50.0, "dislikes": 25.0}
DRIFT = 15.0
STEP = 3.0


def rating_of(value: float) -> str:
    return "strong" if value >= 80 else "interested" if value >= 60 else "neutral" if value >= 40 else "dislikes"


@dataclass
class Prefs:
    enabled: bool
    interest: dict[str, float] = field(default_factory=dict)
    boundaries: set[str] = field(default_factory=set)
    off: set[str] = field(default_factory=set)               # categories switched off by hand
    sources: dict[str, str] = field(default_factory=dict)

    def rating(self, group: str) -> str:
        if group in self.boundaries:
            return "boundary"
        return rating_of(self.interest.get(group, 50.0))

    def by_rating(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {r: [] for r in RATINGS}
        for g in GROUPS:
            if g not in self.off:
                out[self.rating(g)].append(g)
        return out


def validate(raw: Any) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    problems: list[str] = []
    if raw is None:
        return out, problems
    if not isinstance(raw, dict):
        return out, ["intimacy must be an object"]
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for g, r in (raw.get("ratings") or {}).items():
        if g in GROUPS and r in RATINGS:
            out.setdefault("ratings", {})[g] = r
        else:
            problems.append(f"intimacy.ratings.{g}: needs a known group and one of {', '.join(RATINGS)}")
    for g, on in (raw.get("categories") or {}).items():
        if g in GROUPS and isinstance(on, bool):
            out.setdefault("categories", {})[g] = on
        else:
            problems.append(f"intimacy.categories.{g}: needs a known group and true/false")
    return out, problems


def _rng(p: Profile, group: str) -> random.Random:
    digest = hashlib.sha256(f"intimacy:{p.npc_id}:{p.name}:{group}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "little"))


def generated(p: Profile) -> Prefs:
    """Defaults from her generated profile. Every group has its own seeded draw (no group implies another)."""
    g = p.generated or p.values
    get = lambda k, d=50.0: float(g.get(k, d))   # noqa: E731
    comfort, desire, conf = get("intimacy_comfort"), get("libido"), get("sexual_confidence")
    romance = get("romantic_inclination")
    base = {
        "affection": 60 + 0.3 * (comfort - 50) + 0.2 * (romance - 50),
        "hand_holding": 55 + 0.4 * (romance - 50),
        "kissing": 62 + 0.25 * (romance - 50) + 0.2 * (desire - 50),
        "caressing": 58 + 0.3 * (comfort - 50),
        "massage": 55 + 0.2 * (comfort - 50),
        "groping": 42 + 0.3 * (desire - 50) + 0.2 * (comfort - 50),
        "manual": 50 + 0.3 * (desire - 50) + 0.2 * (conf - 50),
        "oral": 45 + 0.3 * (desire - 50) + 0.25 * (conf - 50),
        "intercourse": 58 + 0.35 * (desire - 50),
        "anal": 28 + 0.2 * (desire - 50) + 0.2 * (conf - 50),
        "toys": 35 + 0.25 * (conf - 50),
        "rough": 30 + 0.2 * (desire - 50) + (10 if g.get("relationship_dynamic") in ("leads", "follows") else 0),
        "climax_on_body": 32 + 0.2 * (desire - 50),
        "solo": 30 + 0.3 * (conf - 50),
        "tail": 40.0,
        "vampire": 20.0,
    }
    prefs = Prefs(True)
    for group, b in base.items():
        rng = _rng(p, group)
        prefs.interest[group] = round(max(5.0, min(95.0, b + rng.gauss(0, 14))), 1)
        prefs.sources[group] = "generated"
        if group in ("anal", "rough", "climax_on_body", "vampire", "toys") and rng.random() < 0.25 + get("selectiveness") / 400:
            prefs.boundaries.add(group)
            prefs.sources[group] = "generated boundary"
    return prefs


def _profile_limits(p: Profile, prefs: Prefs) -> None:
    """Limits from her profile as it stands now (hand-set, generated or added over time)."""
    v = p.values
    if "no_rough_handling" in (v.get("boundaries") or []):
        prefs.boundaries.add("rough")
        prefs.sources["rough"] = "her boundary 'no rough handling'"
    if v.get("intimacy_boundary") == "never":
        prefs.boundaries |= SEXUAL
        for s in SEXUAL:
            prefs.sources[s] = "she wants no intimacy"


def resolve(db, p: Profile, manual: dict[str, Any] | None, module_on: bool = True) -> Prefs:
    manual = manual or {}
    if not p.active or not module_on or manual.get("enabled") is False:
        return Prefs(False)
    if p.mode == "manual":
        prefs = Prefs(True, {k: 50.0 for k in GROUPS}, set(), set(), {k: "unset (manual mode)" for k in GROUPS})
    else:
        prefs = generated(p)
        drift: dict[str, float] = {}
        for c in changes_since_reset(db, p.npc_id):
            key = c["attribute"]
            if not key.startswith("intimacy."):
                continue
            group = key.split(".", 1)[1]
            if group not in GROUPS:
                continue
            if c["kind"] == "drift" and c["delta"] is not None:
                drift[group] = drift.get(group, 0.0) + c["delta"]
            elif c["kind"] == "list_add":
                prefs.boundaries.add(group)
                prefs.sources[group] = "boundary added over time"
        for group, d in drift.items():
            d = max(-DRIFT, min(DRIFT, d))
            prefs.interest[group] = round(max(0.0, min(100.0, prefs.interest[group] + d)), 1)
            prefs.sources[group] = "generated + change over time"
    _profile_limits(p, prefs)
    for group, r in (manual.get("ratings") or {}).items():
        if r == "boundary":
            prefs.boundaries.add(group)
        else:
            prefs.boundaries.discard(group)        # only a hand edit can lift a boundary
            prefs.interest[group] = RATING_VALUE[r]
        prefs.sources[group] = "manual"
    for group, on in (manual.get("categories") or {}).items():
        if not on:
            prefs.off.add(group)
            prefs.sources[group] = "switched off by hand"
    return prefs


# ---- change over time ----

def after_scene(db, p: Profile, prefs: Prefs, groups: set[str], feeling: float, now: float, cause: str,
                locked: set[str]) -> list[tuple[str, float]]:
    """She took part in a scene with these groups and felt ``feeling`` (-1..1) about it. Small steps; a
    boundary group is never warmed up, and a group she already disliked that went badly becomes a boundary."""
    if p.mode == "manual" or not prefs.enabled:
        return []
    out = []
    for group in sorted(groups):
        if group in locked or group in prefs.off:
            continue
        if group in prefs.boundaries:
            continue
        if feeling <= -0.5 and prefs.rating(group) == "dislikes":
            add_change(db, p.npc_id, f"intimacy.{group}", "list_add",
                       "she disliked it already and it went badly again", now, value=group, cause=cause)
            out.append((group, 0.0))
            continue
        step = STEP * feeling
        if abs(step) < 0.3:
            continue
        add_change(db, p.npc_id, f"intimacy.{group}", "drift",
                   "a scene she enjoyed" if step > 0 else "a scene she did not enjoy", now, delta=round(step, 2),
                   cause=cause)
        out.append((group, round(step, 2)))
    return out


# ---- prompt lines ----

def preference_lines(prefs: Prefs, private_ok: bool) -> list[str]:
    """For an intimate topic. Enjoyments and dislikes only when she would share private things; her
    will-not-do list always (a limit is stated, never hidden)."""
    if not prefs.enabled:
        return []
    by = prefs.by_rating()
    lines = []
    if private_ok:
        likes = [GROUPS[g] for g in by["strong"]] + [GROUPS[g] for g in by["interested"]]
        if likes:
            lines.append("What she enjoys (she may say so, in her own way): " + ", ".join(likes) + ".")
        if by["dislikes"]:
            lines.append("What she does not care for: " + ", ".join(GROUPS[g] for g in by["dislikes"]) + ".")
    if by["boundary"]:
        lines.append("What she will not do, whatever the mood: " + ", ".join(GROUPS[g] for g in by["boundary"]) + ".")
    return lines


def scene_lines(prefs: Prefs, name: str, groups: set[str]) -> list[str]:
    """While she is in a scene with these groups: what she feels about what is happening."""
    if not prefs.enabled or not groups:
        return []
    lines = []
    over = [GROUPS[g] for g in sorted(groups) if prefs.rating(g) == "boundary" or g in prefs.off]
    disliked = [GROUPS[g] for g in sorted(groups) if prefs.rating(g) == "dislikes" and g not in prefs.off]
    loved = [GROUPS[g] for g in sorted(groups) if prefs.rating(g) == "strong" and g not in prefs.off]
    if over:
        lines.append(f"{name} does not want this: {', '.join(over)} crosses her limit. She says so plainly now and "
                     f"asks for it to stop or change; she does not go along with it.")
    if disliked:
        lines.append(f"{name} is not enjoying {', '.join(disliked)}, and may say so or steer toward something else.")
    if loved:
        lines.append(f"{name} clearly enjoys {', '.join(loved)}.")
    return lines


def to_json(prefs: Prefs) -> dict[str, Any]:
    return {"enabled": prefs.enabled, "ratings": {g: prefs.rating(g) for g in GROUPS if g not in prefs.off},
            "interest": prefs.interest, "off": sorted(prefs.off), "sources": prefs.sources}


def dumps(prefs: Prefs) -> str:
    return json.dumps(to_json(prefs))
