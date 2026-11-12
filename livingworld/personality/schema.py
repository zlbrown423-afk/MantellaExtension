"""What a personality profile holds: every attribute, its kind, range, how sensitive it is and how freely it may change.

This is the one place the attribute list lives. The settings file (``config.py``), generation, change over time,
discovery and the prompt lines all read it, so a new attribute is added here and nowhere else.

Kinds:
* ``number``: 0..100 (50 = average for a Skyrim woman).
* ``choice``: one value from ``choices``.
* ``list``: any number of values from ``choices`` (order kept, no repeats).

``tier`` says how private an attribute is, and so what it takes before she will talk about it (``discovery.py``):
``public`` (shows in how she acts), ``personal`` (she says it to people she likes), ``private`` (only to someone she
trusts and feels close to) and ``boundary`` (a limit she states plainly whenever it comes up).

``drift`` is how far change over time may move a number from where she started (``evolve.py``); 0 = never moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROFILE_VERSION = 1          # the settings file and export format
GENERATOR_VERSION = 1        # bump when generate.py would give someone a different profile

MODES = ("manual", "automatic", "hybrid")
TIERS = ("public", "personal", "private", "boundary")


@dataclass(frozen=True)
class Attribute:
    key: str
    kind: str                       # 'number' | 'choice' | 'list'
    tier: str
    label: str                      # how a person would say it
    drift: float = 0.0              # numbers only: most change over time may move it from the start
    choices: tuple[str, ...] = ()
    words: dict[str, str] = field(default_factory=dict)   # choice/list value -> plain words
    talk: tuple[str, ...] = ()      # words that, said by her, show she spoke about it (discovery.py)


ATTRACTION = {
    "kindness": "kindness", "courage": "courage", "strength": "physical strength", "wit": "a quick wit",
    "humour": "a sense of humour", "confidence": "confidence", "ambition": "ambition", "loyalty": "loyalty",
    "gentleness": "gentleness", "intelligence": "learning and cleverness", "piety": "faith", "wealth": "wealth",
    "honesty": "honesty", "mystery": "a bit of mystery", "magic": "skill with magic", "hard_work": "hard work",
}
DISLIKES = {
    "arrogance": "arrogance", "cruelty": "cruelty", "drunkenness": "drunkenness", "dishonesty": "lies",
    "crude_talk": "crude talk", "pushiness": "pushiness", "cowardice": "cowardice", "poor_hygiene": "dirt and stink",
    "jealousy": "jealousy", "recklessness": "recklessness", "greed": "greed", "lawbreaking": "lawbreakers",
}
BOUNDARIES = {
    "no_public_affection": "no kissing or touching where people can see",
    "no_unasked_touch": "nobody touches her without asking first",
    "no_drunk_advances": "no advances when either of them is drunk",
    "not_with_strangers": "nothing intimate with someone she does not know well",
    "not_with_taken": "nothing with someone who is married or spoken for",
    "no_crude_talk": "no crude talk about bedding",
    "no_rough_handling": "no rough handling",
}
INTIMACY_BOUNDARY = {      # the same words consent.py uses (social/consent.py BOUNDARY_NEEDS)
    "never": "wants no intimacy with anyone",
    "married_only": "only within marriage",
    "committed_only": "only in a committed relationship",
    "after_courtship": "only after a proper courtship",
    "casual_ok": "casual intimacy is fine if she wants the person",
}

ATTRIBUTES: tuple[Attribute, ...] = (
    Attribute("libido", "number", "private", "desire", drift=8,
              talk=("desire", "appetite", "itch", "lust", "needs", "hunger", "frisky", "pent up")),
    Attribute("sexual_confidence", "number", "private", "confidence in intimacy", drift=20,
              talk=("bed", "bedroom", "between the sheets", "know what i want", "experience")),
    Attribute("initiation", "number", "personal", "making the first move", drift=15,
              talk=("first move", "make the first", "ask a man", "wait to be asked", "be asked", "chase")),
    Attribute("selectiveness", "number", "personal", "choosiness", drift=20,
              talk=("choosy", "picky", "particular", "not just anyone", "standards", "high standards")),
    Attribute("privacy", "number", "personal", "privacy about her love life", drift=10,
              talk=("private", "keep it to myself", "nobody's business", "behind closed doors", "discreet")),
    Attribute("romantic_inclination", "number", "public", "romance", drift=15,
              talk=("romance", "romantic", "love", "courting", "flowers", "sweet words")),
    Attribute("intimacy_comfort", "number", "private", "ease with closeness", drift=20,
              talk=("close", "closeness", "comfortable", "touch", "nervous", "shy about")),
    Attribute("exclusivity", "number", "private", "faithfulness", drift=10,
              talk=("faithful", "one man", "one woman", "only one", "loyal to", "share", "other women", "other men")),
    Attribute("social_confidence", "number", "public", "ease with people", drift=15),
    Attribute("openness", "number", "personal", "openness about her wants", drift=20,
              talk=("speak plainly", "honest about", "say what i want", "open about")),
    Attribute("relationship_style", "choice", "public", "what she looks for",
              choices=("romantic", "balanced", "casual"),
              words={"romantic": "a real romance", "balanced": "affection with some lightness", "casual": "something light"},
              talk=("settle down", "marry", "nothing serious", "something light", "fling", "husband", "wife")),
    Attribute("intimacy_pace", "choice", "personal", "pace",
              choices=("slow", "moderate", "quick"),
              words={"slow": "slowly", "moderate": "at an ordinary pace", "quick": "quickly once she is sure"},
              talk=("slow", "take my time", "rush", "patience", "patient", "no hurry")),
    Attribute("relationship_dynamic", "choice", "personal", "who leads",
              choices=("leads", "equal", "follows"),
              words={"leads": "likes to lead", "equal": "wants an equal", "follows": "likes to be led"},
              talk=("take charge", "in charge", "lead", "equal", "partner in")),
    Attribute("intimacy_boundary", "choice", "boundary", "when intimacy is right", choices=tuple(INTIMACY_BOUNDARY),
              words=INTIMACY_BOUNDARY,
              talk=("married", "marriage", "wedding", "committed", "courtship", "court me", "not that kind", "wait")),
    Attribute("attracted_to_sex", "list", "private", "drawn to", choices=("male", "female"),
              words={"male": "men", "female": "women"},
              talk=("men", "women", "a man", "a woman", "lads", "lasses")),
    Attribute("attracted_to", "list", "personal", "what draws her", choices=tuple(ATTRACTION), words=ATTRACTION,
              talk=tuple(ATTRACTION.values()) + ("like in a man", "like in a woman", "draws me", "drawn to")),
    Attribute("dislikes", "list", "personal", "what puts her off", choices=tuple(DISLIKES), words=DISLIKES,
              talk=tuple(DISLIKES.values()) + ("can't stand", "cannot stand", "hate", "puts me off")),
    Attribute("boundaries", "list", "boundary", "her limits", choices=tuple(BOUNDARIES), words=BOUNDARIES,
              talk=("don't touch", "do not touch", "ask first", "hands off", "not in public", "not here", "not when",
                    "drunk", "stranger", "spoken for", "married man", "rough", "crude")),
)
BY_KEY = {a.key: a for a in ATTRIBUTES}
NUMBERS = tuple(a.key for a in ATTRIBUTES if a.kind == "number")


def validate_value(key: str, value: Any) -> Any:
    """``value`` made valid for ``key``, or ValueError saying what is wrong (for the settings file and imports)."""
    attr = BY_KEY.get(key)
    if attr is None:
        raise ValueError(f"unknown attribute '{key}' (known: {', '.join(BY_KEY)})")
    if attr.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number 0-100, not {value!r}")
        if not 0 <= value <= 100:
            raise ValueError(f"{key} must be between 0 and 100, not {value}")
        return round(float(value), 1)
    if attr.kind == "choice":
        if value not in attr.choices:
            raise ValueError(f"{key} must be one of {', '.join(attr.choices)}, not {value!r}")
        return value
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of {', '.join(attr.choices)}")
    out: list[str] = []
    for item in value:
        if item not in attr.choices:
            raise ValueError(f"{key}: {item!r} is not one of {', '.join(attr.choices)}")
        if item not in out:
            out.append(item)
    return out


def describe_number(value: float) -> str:
    if value >= 80:
        return "very high"
    if value >= 62:
        return "high"
    if value > 38:
        return "middling"
    if value > 20:
        return "low"
    return "very low"


def json_schema() -> dict[str, Any]:
    """A JSON Schema for the settings file (written to data/personality.schema.json by the admin tool)."""
    def value_schema(a: Attribute) -> dict[str, Any]:
        if a.kind == "number":
            return {"type": "number", "minimum": 0, "maximum": 100, "description": a.label}
        if a.kind == "choice":
            return {"enum": list(a.choices), "description": a.label}
        return {"type": "array", "items": {"enum": list(a.choices)}, "uniqueItems": True, "description": a.label}

    values = {"type": "object", "additionalProperties": False,
              "properties": {a.key: value_schema(a) for a in ATTRIBUTES}}
    npc = {"type": "object", "additionalProperties": False, "properties": {
        "enabled": {"type": "boolean"},
        "mode": {"enum": list(MODES)},
        "values": values,
        "private": {"type": "array", "items": {"type": "string"},
                    "description": "attributes (or list items as 'boundaries:no_unasked_touch') she never shares"},
        "adult_confirmed": {"type": "boolean",
                            "description": "she is an adult although the game gives no race; never overrides a child"},
        "plugin": {"type": "string"}, "base_id": {"type": "string"}, "note": {"type": "string"}}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Living World personality profiles",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "version": {"const": PROFILE_VERSION},
            "enabled": {"type": "boolean"},
            "default_mode": {"enum": list(MODES)},
            "evolution": {"type": "object", "additionalProperties": False, "properties": {
                "enabled": {"type": "boolean"}, "rate": {"type": "number", "minimum": 0, "maximum": 3}}},
            "baseline": {"type": "object", "additionalProperties": False,
                         "properties": {k: {"type": "number", "minimum": -50, "maximum": 50} for k in NUMBERS},
                         "description": "points added to every generated profile's numbers"},
            "npcs": {"type": "object", "additionalProperties": npc,
                     "description": "keyed by name ('Gerdur') or 'Plugin.esm|0x0001347C'"},
        },
    }
