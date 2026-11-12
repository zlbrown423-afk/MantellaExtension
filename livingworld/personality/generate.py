"""AUTOMATIC profiles: each woman's starting profile, worked out from who she already is. No LLM call.

Inputs, in order of weight:
1. The hand-written romance seed (``social/data/*.json`` "romance"), when she is in it: drive, romantic, shy,
   flirtatious, monogamy, intimacy_boundary and attracted_to map one to one (drive 0.65 -> libido 65), so the
   romance simulation sees exactly the numbers it had before profiles existed.
2. Her personality traits (``social/traits.py``: warmth, reserve, modesty, bold, anxiety, loyal, honest...).
3. Her work and standing, from her name and Mantella bio (a priestess, a bard, a jarl...).
4. Her relationships: a married woman leans faithful unless the seed says otherwise.
5. Her own hygiene standard (``hygiene/profile.py``): the fussy dislike dirt.
6. A draw seeded with her plugin and form id, so two women with the same bio differ, and the same woman is the same
   in every save and every run. Nothing is ever re-rolled between conversations.

Every value records where it came from (``sources``), shown by the admin tool's inspect.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

from livingworld.personality import schema

WORK_NUDGES: tuple[tuple[tuple[str, ...], dict[str, float]], ...] = (
    (("priest", "priestess", "temple", "pious", "devout", "acolyte", "vigilant"),
     {"exclusivity": 10, "privacy": 10, "libido": -8, "intimacy_comfort": -8}),
    (("bard", "bards", "singer", "minstrel", "performer"),
     {"social_confidence": 15, "romantic_inclination": 10, "openness": 10}),
    (("warrior", "companion", "companions", "mercenary", "sellsword", "adventurer", "soldier", "guard", "housecarl"),
     {"sexual_confidence": 8, "initiation": 8, "social_confidence": 5}),
    (("jarl", "steward", "thane", "noble", "court", "courtier", "lady"),
     {"privacy": 10, "selectiveness": 15}),
    (("innkeeper", "barmaid", "barkeep", "server", "tavern"),
     {"social_confidence": 10, "openness": 5}),
    (("widow", "widowed", "grieving", "mourning"),
     {"libido": -5, "romantic_inclination": -5, "selectiveness": 8}),
    (("thief", "thieves", "smuggler", "pirate", "outlaw", "bandit"),
     {"exclusivity": -8, "privacy": 8}),
    (("mage", "wizard", "scholar", "alchemist", "college", "court wizard"),
     {"selectiveness": 8}),
)
LEARNED_WORDS = ("mage", "wizard", "scholar", "college", "alchemist", "sorceress", "enchanter", "court wizard")


@dataclass
class Generated:
    values: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)


def _clamp(x: float) -> float:
    return round(max(3.0, min(97.0, x)), 1)


def _person(db, npc_id: int) -> dict[str, Any]:
    row = db.execute("SELECT display_name, plugin, base_form_id, sex FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    persona = db.execute("SELECT bio FROM persona_version WHERE npc_id = ? ORDER BY version DESC LIMIT 1",
                         (npc_id,)).fetchone()
    return {"name": row[0], "plugin": row[1], "base": row[2], "sex": row[3], "bio": persona[0] if persona else ""}


def _seeded(db, person: dict[str, Any]) -> dict[str, Any]:
    from livingworld.social.seed import _local_id, romance_traits
    key = (person["plugin"], _local_id(str(person["base"]).replace("0x", "").replace("0X", "")))
    return dict(romance_traits().get(key) or {})


def _married(db, npc_id: int) -> bool:
    if db.execute("SELECT 1 FROM relationship WHERE from_id = ? AND kinship = 'spouse' LIMIT 1", (npc_id,)).fetchone():
        return True
    return db.execute("SELECT 1 FROM v_romance_bond WHERE (a_id = ? OR b_id = ?) AND stage = 'married' LIMIT 1",
                      (npc_id, npc_id)).fetchone() is not None


def _hygiene_standard(db, npc_id: int) -> float:
    try:
        from livingworld.hygiene.profile import habits_for
        return habits_for(db, npc_id).hygiene_standard
    except Exception:
        return 0.5


def _pick(rng: random.Random, weights: dict[str, float], low: int, high: int) -> list[str]:
    """``low``..``high`` distinct keys, more likely the heavier ones (weights > 0)."""
    pool = {k: max(0.05, w) for k, w in weights.items()}
    count = rng.randint(low, high)
    out: list[str] = []
    while pool and len(out) < count:
        total = sum(pool.values())
        roll = rng.uniform(0, total)
        for k, w in pool.items():
            roll -= w
            if roll <= 0:
                out.append(k)
                del pool[k]
                break
    return out


def generate(db, npc_id: int) -> Generated:
    """Her starting profile. The same inputs always give the same profile."""
    from livingworld.social.traits import traits_of
    person = _person(db, npc_id)
    t = traits_of(db, npc_id)
    seed = _seeded(db, person)
    words = set(re.findall(r"[a-z]+", f"{person['name']} {person['bio']}".lower()))
    rng = random.Random(f"personality:{person['plugin']}:{person['base']}")
    g = lambda sd: rng.gauss(0, sd)   # noqa: E731
    src: dict[str, str] = {}
    v: dict[str, Any] = {}

    def number(key: str, seed_key: str | None, seed_map, formula: float) -> None:
        if seed_key and isinstance(seed.get(seed_key), (int, float)):
            v[key] = round(float(seed_map(seed[seed_key])) * 100, 1)
            src[key] = f"romance seed '{seed_key}'"
        else:
            v[key] = _clamp(formula)
            src[key] = "traits, work and a personal draw"

    married = _married(db, npc_id)
    number("libido", "drive", lambda x: x, 50 + 20 * (0.5 - t["modesty"]) + 10 * (t["bold"] - 0.5) + g(14))
    number("sexual_confidence", "shy", lambda x: 1 - x,
           50 + 30 * (t["bold"] - 0.5) + 25 * (0.5 - t["anxiety"]) + 15 * (0.5 - t["modesty"]) + g(10))
    conf = v["sexual_confidence"]
    number("initiation", "flirtatious", lambda x: x,
           40 + 0.3 * (conf - 50) + 30 * (t["dominance"] - 0.5) + 20 * (t["bold"] - 0.5) + g(10))
    number("selectiveness", None, None,
           55 + 30 * (t["reserve"] - 0.5) + 20 * (t["modesty"] - 0.5) - 20 * (t["warmth"] - 0.5)
           + 15 * (0.5 - t["trusting"]) + g(10))
    number("privacy", None, None,
           50 + 40 * (t["reserve"] - 0.5) + 30 * (t["modesty"] - 0.5) - 20 * (t["gossipy"] - 0.5) + g(8))
    number("romantic_inclination", "romantic", lambda x: x,
           50 + 30 * (t["warmth"] - 0.5) + 10 * (t["family"] - 0.5) + g(12))
    number("intimacy_comfort", None, None,
           50 + 30 * (0.5 - t["modesty"]) + 25 * (0.5 - t["anxiety"]) + 20 * (0.5 - t["personal_space"]) + g(10))
    number("exclusivity", "monogamy", lambda x: x,
           68 + 30 * (t["loyal"] - 0.5) + 20 * (t["family"] - 0.5) + 15 * (t["honor"] - 0.5)
           + (10 if married else 0) + g(12))
    number("social_confidence", None, None,
           50 + 35 * (t["bold"] - 0.5) + 25 * (t["warmth"] - 0.5) - 30 * (t["anxiety"] - 0.5)
           - 15 * (t["reserve"] - 0.5) + g(8))
    number("openness", None, None,
           50 + 30 * (t["honest"] - 0.5) - 35 * (t["reserve"] - 0.5) - 20 * (t["modesty"] - 0.5)
           + 10 * (t["warmth"] - 0.5) + g(10))
    for keys, nudges in WORK_NUDGES:
        if words & set(keys):
            for key, n in nudges.items():
                if src[key].startswith("romance seed"):
                    continue
                v[key] = _clamp(v[key] + n)
                src[key] += f" + {sorted(words & set(keys))[0]}"

    rom, excl = v["romantic_inclination"], v["exclusivity"]
    v["relationship_style"] = "romantic" if rom >= 60 and excl >= 55 else ("casual" if rom < 40 and excl < 55 else "balanced")
    src["relationship_style"] = "romance and faithfulness"
    pace = v["intimacy_comfort"] + conf - v["selectiveness"]
    v["intimacy_pace"] = "slow" if pace < 35 else ("quick" if pace > 70 else "moderate")
    src["intimacy_pace"] = "ease with closeness, confidence and choosiness"
    dom = t["dominance"] + rng.uniform(-0.04, 0.04)
    v["relationship_dynamic"] = "leads" if dom >= 0.6 else ("follows" if dom <= 0.4 else "equal")
    src["relationship_dynamic"] = "dominance trait"

    if seed.get("intimacy_boundary") in schema.INTIMACY_BOUNDARY:
        v["intimacy_boundary"], src["intimacy_boundary"] = seed["intimacy_boundary"], "romance seed 'intimacy_boundary'"
    else:
        pious = bool(words & {"priest", "priestess", "temple", "pious", "devout", "acolyte"})
        if pious or t["modesty"] >= 0.72:
            b = "married_only"
        elif excl >= 75:
            b = "committed_only"
        elif excl >= 50 or v["intimacy_comfort"] < 55:
            b = "after_courtship"
        else:
            b = "casual_ok"
        v["intimacy_boundary"], src["intimacy_boundary"] = b, "modesty, faith and faithfulness"

    if isinstance(seed.get("attracted_to"), list) and seed["attracted_to"]:
        v["attracted_to_sex"] = [s for s in seed["attracted_to"] if s in ("male", "female")]
        src["attracted_to_sex"] = "romance seed 'attracted_to'"
    else:
        roll = rng.random()
        v["attracted_to_sex"] = ["female"] if roll < 0.05 else (["male", "female"] if roll < 0.12 else ["male"])
        src["attracted_to_sex"] = "personal draw"

    learned = 1.0 if words & set(LEARNED_WORDS) else 0.0
    v["attracted_to"] = _pick(rng, {
        "kindness": t["warmth"], "gentleness": 0.5 * t["warmth"] + 0.5 * t["anxiety"], "humour": t["warmth"] * 0.8,
        "courage": t["honor"], "strength": 0.5 * t["honor"] + 0.4 * t["bold"], "loyalty": t["loyal"],
        "honesty": t["honest"] * 0.8 + 0.2 * t["law"], "confidence": t["bold"] * 0.8,
        "ambition": t["wealth"] * 0.7 + t["dominance"] * 0.3, "wealth": t["wealth"] * 0.5 + t["greedy"] * 0.5,
        "hard_work": t["wealth"] * 0.4 + t["family"] * 0.4, "wit": 0.5, "piety": t["modesty"] * 0.8,
        "intelligence": 0.3 + 0.8 * learned, "magic": 0.1 + 0.9 * learned, "mystery": t["reserve"] * 0.6}, 2, 3)
    src["attracted_to"] = "weighted by her traits and work, personal draw"
    hygiene = _hygiene_standard(db, npc_id)
    v["dislikes"] = _pick(rng, {
        "crude_talk": t["modesty"], "drunkenness": t["modesty"] * 0.6 + t["law"] * 0.3, "lawbreaking": t["law"] * 0.9,
        "dishonesty": t["honest"] * 0.7 + (1 - t["trusting"]) * 0.3, "cruelty": t["warmth"],
        "cowardice": t["honor"] * 0.9, "arrogance": t["warmth"] * 0.5 + (1 - t["dominance"]) * 0.4,
        "pushiness": t["personal_space"] * 0.8, "poor_hygiene": max(0.05, hygiene - 0.2),
        "jealousy": (1 - t["temper"]) * 0.5, "recklessness": t["anxiety"] * 0.7, "greed": (1 - t["greedy"]) * 0.6},
        2, 3)
    src["dislikes"] = "weighted by her traits and hygiene standard, personal draw"
    bounds: list[str] = []
    if v["privacy"] >= 65:
        bounds.append("no_public_affection")
    if v["intimacy_comfort"] <= 40 or t["personal_space"] >= 0.65:
        bounds.append("no_unasked_touch")
    if excl >= 70:
        bounds.append("not_with_taken")
    if v["selectiveness"] >= 65:
        bounds.append("not_with_strangers")
    if "crude_talk" in v["dislikes"] and t["modesty"] >= 0.6:
        bounds.append("no_crude_talk")
    if "drunkenness" in v["dislikes"] or rng.random() < 0.25:
        bounds.append("no_drunk_advances")
    v["boundaries"] = bounds
    src["boundaries"] = "privacy, ease with closeness, faithfulness, choosiness and dislikes"
    for key in schema.BY_KEY:
        v[key] = schema.validate_value(key, v[key])
    return Generated(v, src)


def store_generated(db, npc_id: int, gt: float, replace: bool = False) -> Generated:
    """Generate and keep her starting profile (once; again only when ``replace``)."""
    row = db.execute("SELECT generated_json, sources_json FROM personality_profile WHERE npc_id = ?", (npc_id,)).fetchone()
    if row and not replace:
        return Generated(json.loads(row[0]), json.loads(row[1]))
    gen = generate(db, npc_id)
    db.execute("INSERT INTO personality_profile (npc_id, generator_version, generated_json, sources_json, created_gt)"
               " VALUES (?,?,?,?,?) ON CONFLICT(npc_id) DO UPDATE SET generator_version = excluded.generator_version,"
               " generated_json = excluded.generated_json, sources_json = excluded.sources_json,"
               " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
               (npc_id, schema.GENERATOR_VERSION, json.dumps(gen.values), json.dumps(gen.sources), gt))
    return gen
