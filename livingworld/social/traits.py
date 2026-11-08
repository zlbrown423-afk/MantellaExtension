"""Personality traits (0..1, 0.5 = average), kept in ``persona_version.traits_json``.

Traits are seeded once per person from their Mantella character bio by simple word rules (no LLM call,
so it costs nothing): "a gossip" raises ``gossipy``, "honest" raises ``honest``, "thief" lowers it and
``law``, and so on, plus a small fixed per-person nudge so two people with the same bio are not
identical. A hand-written seed file (``social/data/*.json``) wins for any trait it sets.

Traits the simulation reads: gossipy (passing news on), honest (how faithfully news is retold),
trusting (how readily hearsay is believed), loyal (how hard harm to friends is taken), and the values
law, family, wealth, honor, modesty (how much each area of someone's reputation matters to them).
"""
from __future__ import annotations

import hashlib
import json
import re

TRAITS = ("gossipy", "honest", "trusting", "loyal", "bold", "greedy", "temper", "law", "family", "wealth", "honor",
          "modesty", "warmth", "reserve")

# (trait, change, words): each word found in the bio moves the trait by change (capped at +-0.35 per trait)
RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("gossipy", 0.2, ("gossip", "gossips", "gossipy", "chatty", "talkative", "nosy", "rumor", "rumors", "rumour",
                      "rumours", "busybody", "bard", "storyteller", "chatter")),
    ("gossipy", -0.2, ("quiet", "reserved", "secretive", "taciturn", "private", "solitary", "loner", "aloof")),
    ("honest", 0.2, ("honest", "truthful", "honorable", "honourable", "upright", "sincere", "fair")),
    ("honest", -0.25, ("liar", "lies", "lying", "deceit", "deceitful", "scheming", "schemer", "thief", "thieves",
                       "con", "swindler", "cheat", "smuggler", "fence", "manipulative", "dishonest")),
    ("trusting", 0.2, ("naive", "trusting", "gullible", "kind-hearted", "innocent")),
    ("trusting", -0.2, ("suspicious", "paranoid", "distrustful", "wary", "cynical", "guarded")),
    ("loyal", 0.2, ("loyal", "devoted", "faithful", "steadfast", "dutiful")),
    ("bold", 0.2, ("brave", "bold", "fearless", "warrior", "adventurous", "proud")),
    ("bold", -0.2, ("timid", "cowardly", "shy", "nervous", "fearful", "meek")),
    ("greedy", 0.2, ("greedy", "greed", "avaricious", "miser", "miserly", "profit", "money", "coin")),
    ("temper", 0.2, ("temper", "angry", "hot-headed", "hotheaded", "short-tempered", "irritable", "gruff", "brash",
                     "violent", "aggressive")),
    ("temper", -0.15, ("calm", "patient", "gentle", "easygoing", "even-tempered")),
    ("law", 0.25, ("guard", "guards", "legion", "legionnaire", "soldier", "justice", "law", "imperial", "steward",
                   "housecarl", "jarl", "thane", "captain")),
    ("law", -0.25, ("thief", "thieves", "bandit", "smuggler", "outlaw", "criminal", "assassin", "skooma", "fence",
                    "brotherhood", "forsworn")),
    ("family", 0.2, ("family", "husband", "wife", "children", "son", "daughter", "mother", "father", "brother",
                     "sister", "widow", "widower")),
    ("wealth", 0.2, ("merchant", "trader", "shop", "shopkeeper", "rich", "wealthy", "business", "gold", "trade",
                     "innkeeper", "profit")),
    ("honor", 0.2, ("honor", "honour", "warrior", "companions", "pride", "duty", "noble", "knight")),
    ("modesty", 0.2, ("pious", "priest", "priestess", "temple", "devout", "modest", "proper", "prim")),
    ("warmth", 0.2, ("kind", "friendly", "warm", "cheerful", "welcoming", "generous", "caring")),
    ("warmth", -0.2, ("cold", "rude", "surly", "bitter", "arrogant", "mean", "unfriendly")),
    ("reserve", 0.2, ("reserved", "quiet", "shy", "private", "aloof", "stoic")),
)
MAX_SHIFT = 0.35
NUDGE = 0.06


def from_bio(bio: str, who: str = "") -> dict[str, float]:
    """Traits from bio words, plus a small fixed nudge per person (``who`` = a stable key, e.g. form ID)."""
    words = set(re.findall(r"[a-z][a-z\-]*", (bio or "").lower()))
    shift: dict[str, float] = {t: 0.0 for t in TRAITS}
    for trait, change, keys in RULES:
        hits = len(words & set(keys))
        if hits:
            shift[trait] += change * min(hits, 2)
    out = {}
    for trait in TRAITS:
        digest = hashlib.sha256(f"{who}:{trait}".encode()).digest()
        nudge = (digest[0] / 255.0 - 0.5) * 2 * NUDGE if who else 0.0
        value = 0.5 + max(-MAX_SHIFT, min(MAX_SHIFT, shift[trait])) + nudge
        out[trait] = round(max(0.05, min(0.95, value)), 2)
    return out


def traits_of(db, npc_id: int) -> dict[str, float]:
    """Every trait for one person (0.5 where nothing is known)."""
    row = db.execute("SELECT traits_json FROM persona_version WHERE npc_id = ? ORDER BY version DESC LIMIT 1",
                     (npc_id,)).fetchone()
    out = {t: 0.5 for t in TRAITS}
    if row:
        try:
            for key, value in json.loads(row[0] or "{}").items():
                if isinstance(value, (int, float)):
                    out[key] = float(value)
        except (ValueError, TypeError, AttributeError):
            pass
    return out


def seed_traits_from_bio(db, npc_id: int, bio: str, gt: float) -> bool:
    """Give someone traits from their bio the first time they are seen. Keeps any traits already set
    (a seed file's, or ones written by hand) and only adds the missing ones. Returns True when written."""
    if not bio:
        return False
    row = db.execute("SELECT persona_id, bio, traits_json, source FROM persona_version WHERE npc_id = ?"
                     " ORDER BY version DESC LIMIT 1", (npc_id,)).fetchone()
    ident = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    derived = from_bio(bio, f"{ident[0]}:{ident[1]}" if ident else str(npc_id))
    if row is None:
        db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                   " VALUES (?, 1, ?, ?, 'mantella_csv', ?)", (npc_id, bio, json.dumps(derived), gt))
        return True
    try:
        current = json.loads(row[2] or "{}")
    except ValueError:
        current = {}
    missing = {k: v for k, v in derived.items() if k not in current}
    if not missing and row[1]:
        return False
    current.update(missing)
    db.execute("UPDATE persona_version SET traits_json = ?, bio = CASE WHEN bio = '' THEN ? ELSE bio END"
               " WHERE persona_id = ?", (json.dumps(current), bio, row[0]))
    return True
