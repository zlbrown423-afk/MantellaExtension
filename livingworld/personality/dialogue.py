"""Dialogue personality: how she talks when attraction, flirting or intimacy come up. Toggles, 0..100 sliders and a
phrase library per woman, all of which are style guidance for the AI, never lines to repeat.

Precedence, strongest first (the brief's order):
1. Hard limits: a toggle switched off (by hand, or by her own boundaries/dislikes), and forbidden vocabulary.
   A toggle switched off by hand stays off whatever anything else says.
2. Hand-set values in the settings file (``"dialogue": {...}`` under her entry).
3. Her persistent preferences: her profile (``profile.py``) feeds the generated sliders.
4. Change over time: sliders follow the change in the attribute each comes from (confidence <- sexual_confidence...).
5. Generated defaults: worked out from her generated profile and traits, seeded by her form id (never re-rolled).

Settings file shape, per woman (every part optional):

    "dialogue": {
      "enabled": true,
      "toggles": {"suggestive": false, "teasing": true},
      "sliders": {"flirtation": 70, "privacy": 80},
      "vocabulary": {"terms_of_affection": ["my wolf"], "custom": ["by Dibella's grace"]},
      "remove": {"teasing": ["you're trouble"]}
    }

Vocabulary: generated phrases + hand-added ones - removed ones, without duplicates (case and spacing ignored).
``forbidden`` phrases are never said; ``disliked`` ones she avoids and dislikes hearing.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

from livingworld.personality import schema
from livingworld.personality.profile import Profile

TOGGLES: dict[str, str] = {
    "flirtatious": "flirtatious talk",
    "teasing": "playful teasing",
    "suggestive": "suggestive remarks",
    "romantic_expressions": "romantic expressions",
    "direct_attraction": "saying outright that she is attracted",
    "coy": "coy, reserved answers",
    "confident": "confident talk",
    "assertive": "assertive talk",
    "dominant_tone": "a dominant tone",
    "submissive_tone": "a yielding tone",
    "double_entendres": "double entendres",
    "intimate_compliments": "intimate compliments",
    "initiate_flirtation": "starting flirtation herself",
    "initiate_intimate_talk": "starting intimate conversations herself",
    "discuss_preferences": "talking about what she likes",
    "private_vocabulary": "her own pet phrases",
}
SLIDERS: dict[str, tuple[str, str | None]] = {   # key -> (meaning, profile attribute it follows over time)
    "flirtation": ("how often and how much she flirts", "initiation"),
    "suggestiveness": ("how suggestive she gets", "libido"),
    "playfulness": ("how much she teases and banters", None),
    "confidence": ("how directly she shows attraction", "sexual_confidence"),
    "romantic_expression": ("how often she uses affectionate language", "romantic_inclination"),
    "assertiveness": ("how strongly she steers the talk", "initiation"),
    "vocabulary_variety": ("how varied her expressions are", None),
    "initiative": ("how likely she is to raise these subjects herself", "initiation"),
    "privacy": ("how much she keeps intimate talk out of public hearing", "privacy"),
}
CATEGORIES: dict[str, str] = {
    "expressions": "favourite expressions",
    "terms_of_affection": "terms of affection",
    "flirtatious": "flirtatious phrases",
    "teasing": "teasing",
    "romantic": "romantic phrases",
    "suggestive": "suggestive language",
    "disliked": "expressions she dislikes",
    "forbidden": "words she never uses",
    "custom": "phrases of her own",
}
# small, lore-friendly starting library; each woman gets a seeded handful, never the whole list
LIBRARY: dict[str, tuple[str, ...]] = {
    "expressions": ("by the Nine", "Divines' mercy", "well now", "Shor's bones", "gods, listen to me", "mark my words",
                    "talos guide you", "by Mara", "isn't that something"),
    "terms_of_affection": ("love", "my heart", "dear one", "sweetling", "my dear", "handsome", "my warrior",
                           "you fool", "darling", "my bear"),
    "flirtatious": ("you've a way about you", "I could get used to this", "keep looking at me like that",
                    "you clean up well", "stay a while", "I'd not mind the company"),
    "teasing": ("you're trouble", "don't flatter yourself", "is that the best you've got", "careful now",
                "you wish", "big talk from you"),
    "romantic": ("you make the cold bearable", "I think of you more than I should", "stay by me",
                 "you're a light in this place", "I'd follow you to Sovngarde"),
    "suggestive": ("perhaps somewhere quieter", "the night is young", "my room is warm", "you'd like to know, wouldn't you",
                   "come closer and find out"),
}
TALKY = {"flirtatious", "teasing", "suggestive", "double_entendres", "intimate_compliments", "direct_attraction",
         "initiate_flirtation", "initiate_intimate_talk"}


@dataclass
class Dialogue:
    enabled: bool
    toggles: dict[str, bool] = field(default_factory=dict)
    sliders: dict[str, float] = field(default_factory=dict)
    vocabulary: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)     # 'toggles.x' / 'sliders.x' -> manual / generated / ...


def validate(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """The settings file's "dialogue" object made valid; problems listed, bad parts skipped."""
    out: dict[str, Any] = {}
    problems: list[str] = []
    if raw is None:
        return out, problems
    if not isinstance(raw, dict):
        return out, ["dialogue must be an object"]
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for k, v in (raw.get("toggles") or {}).items():
        if k in TOGGLES and isinstance(v, bool):
            out.setdefault("toggles", {})[k] = v
        else:
            problems.append(f"dialogue.toggles.{k}: needs a known toggle and true/false")
    for k, v in (raw.get("sliders") or {}).items():
        if k in SLIDERS and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100:
            out.setdefault("sliders", {})[k] = float(v)
        else:
            problems.append(f"dialogue.sliders.{k}: needs a known slider and 0-100")
    for part in ("vocabulary", "remove"):
        for cat, phrases in (raw.get(part) or {}).items():
            if cat not in CATEGORIES or not isinstance(phrases, list):
                problems.append(f"dialogue.{part}.{cat}: needs a known category and a list of phrases")
                continue
            clean = _dedupe([str(p) for p in phrases if str(p).strip()])
            out.setdefault(part, {})[cat] = clean
    return out, problems


def _norm(phrase: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", phrase.lower()))


def _dedupe(phrases: list[str]) -> list[str]:
    seen, out = set(), []
    for p in phrases:
        n = _norm(p)
        if n and n not in seen:
            seen.add(n)
            out.append(" ".join(p.split()))
    return out


def _rng(p: Profile, what: str) -> random.Random:
    digest = hashlib.sha256(f"dialogue:{p.npc_id}:{p.name}:{what}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "little"))


def _clamp(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 1)


def generated(p: Profile, traits: dict[str, float]) -> Dialogue:
    """Defaults from her generated profile (so they never shift between conversations), plus a seeded draw."""
    g = p.generated or p.values
    get = lambda k, d=50.0: float(g.get(k, d))   # noqa: E731
    rng = _rng(p, "sliders")
    warmth, modesty, dominance = traits.get("warmth", .5), traits.get("modesty", .5), traits.get("dominance", .5)
    s = {
        "flirtation": _clamp(0.5 * get("initiation") + 0.3 * get("social_confidence") + 20 * (warmth - .5) + rng.gauss(0, 6)),
        "suggestiveness": _clamp(0.45 * get("libido") + 0.35 * get("openness") - 30 * (modesty - .5)
                                 - 0.15 * (get("privacy") - 50) + rng.gauss(0, 6)),
        "playfulness": _clamp(50 + 40 * (warmth - .5) + 0.2 * (get("social_confidence") - 50) + rng.gauss(0, 10)),
        "confidence": _clamp(0.8 * get("sexual_confidence") + 0.2 * get("social_confidence")),
        "romantic_expression": _clamp(0.85 * get("romantic_inclination") + rng.gauss(0, 5)),
        "assertiveness": _clamp(0.5 * get("initiation") + 50 * dominance + rng.gauss(0, 6)),
        "vocabulary_variety": _clamp(50 + rng.gauss(0, 15)),
        "initiative": _clamp(0.8 * get("initiation") + 0.2 * get("social_confidence")),
        "privacy": _clamp(get("privacy")),
    }
    dyn = g.get("relationship_dynamic", "equal")
    dislikes = set(g.get("dislikes", []))
    crude_off = "crude_talk" in dislikes or "no_crude_talk" in g.get("boundaries", [])
    t = {
        "flirtatious": s["flirtation"] >= 40,
        "teasing": s["playfulness"] >= 50,
        "suggestive": s["suggestiveness"] >= 50 and not crude_off,
        "romantic_expressions": s["romantic_expression"] >= 45,
        "direct_attraction": s["confidence"] >= 60,
        "coy": s["confidence"] < 45,
        "confident": s["confidence"] >= 55,
        "assertive": s["assertiveness"] >= 60,
        "dominant_tone": dyn == "leads",
        "submissive_tone": dyn == "follows",
        "double_entendres": s["suggestiveness"] >= 55 and s["playfulness"] >= 50 and not crude_off,
        "intimate_compliments": s["suggestiveness"] >= 40 and s["confidence"] >= 45,
        "initiate_flirtation": s["initiative"] >= 55 and s["flirtation"] >= 45,
        "initiate_intimate_talk": s["initiative"] >= 65 and s["suggestiveness"] >= 55,
        "discuss_preferences": get("openness") >= 45,
        "private_vocabulary": True,
    }
    vrng = _rng(p, "vocabulary")
    vocab: dict[str, list[str]] = {}
    want = {"expressions": 2, "terms_of_affection": 2, "flirtatious": 2 if t["flirtatious"] else 0,
            "teasing": 2 if t["teasing"] else 0, "romantic": 2 if t["romantic_expressions"] else 0,
            "suggestive": 2 if t["suggestive"] else 0}
    extra = 1 if s["vocabulary_variety"] >= 60 else 0
    for cat, n in want.items():
        if n:
            vocab[cat] = vrng.sample(list(LIBRARY[cat]), min(len(LIBRARY[cat]), n + extra))
    return Dialogue(True, t, s, vocab, {**{f"toggles.{k}": "generated" for k in t},
                                        **{f"sliders.{k}": "generated" for k in s}})


def resolve(p: Profile, traits: dict[str, float], manual: dict[str, Any] | None, module_on: bool = True) -> Dialogue:
    """Her dialogue personality now, by the precedence above."""
    manual = manual or {}
    if not p.active or not module_on or manual.get("enabled") is False:
        return Dialogue(False)
    if p.mode == "manual":
        d = Dialogue(True, {k: False for k in TOGGLES}, {}, {}, {})
        d.toggles["private_vocabulary"] = True
    else:
        d = generated(p, traits)
        for key, (_, follows) in SLIDERS.items():       # change over time, through the attribute it follows
            if follows and follows in p.drift and not (p.locked(follows)):
                d.sliders[key] = _clamp(d.sliders[key] + p.drift[follows])
                d.sources[f"sliders.{key}"] = "generated + change over time"
            elif follows and p.locked(follows) and follows in p.values and follows in p.generated:
                shift = p.values[follows] - p.generated[follows]       # her hand-set profile value moves it too
                if shift:
                    d.sliders[key] = _clamp(d.sliders[key] + shift)
                    d.sources[f"sliders.{key}"] = "generated, following a hand-set profile value"
    for k, v in (manual.get("sliders") or {}).items():
        d.sliders[k], d.sources[f"sliders.{k}"] = v, "manual"
    for k, v in (manual.get("toggles") or {}).items():
        d.toggles[k], d.sources[f"toggles.{k}"] = v, "manual"
    for cat, phrases in (manual.get("vocabulary") or {}).items():
        d.vocabulary[cat] = _dedupe(d.vocabulary.get(cat, []) + phrases)
    for cat, phrases in (manual.get("remove") or {}).items():
        gone = {_norm(x) for x in phrases}
        d.vocabulary[cat] = [x for x in d.vocabulary.get(cat, []) if _norm(x) not in gone]
    forbidden = {_norm(x) for x in d.vocabulary.get("forbidden", [])}
    for cat in list(d.vocabulary):
        if cat != "forbidden":
            d.vocabulary[cat] = [x for x in d.vocabulary[cat] if _norm(x) not in forbidden]
    d.vocabulary = {c: v for c, v in d.vocabulary.items() if v}
    return d


@dataclass(frozen=True)
class Moment:
    trust: float = 0.0
    attraction: float = 0.0
    others_listening: bool = False
    mood_score: int | None = None
    bonded_to_player: bool = False


def license_for(d: Dialogue, m: Moment) -> float:
    """0..1: how far her flirting may go right now (trust, attraction, company and mood all weigh in)."""
    base = (0.4 * d.sliders.get("flirtation", 50) + 0.6 * d.sliders.get("suggestiveness", 50)) / 100
    comfort = 0.35 + 0.4 * max(0.0, m.trust) + 0.25 * m.attraction + (0.2 if m.bonded_to_player else 0.0)
    if m.others_listening:
        comfort -= 0.4 * d.sliders.get("privacy", 50) / 100
    if m.mood_score is not None:
        comfort += (m.mood_score - 5) * 0.05
    return round(max(0.0, min(1.0, base * comfort * 1.6)), 3)


def style_lines(d: Dialogue, name: str, player_name: str, m: Moment, topic: bool, salt: str = "") -> list[str]:
    """Lines for the prompt. Without a romantic topic only a short manner line; with one, the full guidance."""
    if not d.enabled:
        return []
    on = [TOGGLES[k] for k, v in d.toggles.items() if v and k != "private_vocabulary"]
    off = [TOGGLES[k] for k, v in d.toggles.items() if not v and k != "private_vocabulary"]
    lic = license_for(d, m)
    if lic >= 0.6:
        now = f"with {player_name} right now she can let it show freely"
    elif lic >= 0.35:
        now = f"with {player_name} right now she keeps it light: hints and warmth, nothing bold"
    else:
        now = f"with {player_name} right now she stays polite; any interest shows only indirectly"
    if m.mood_score is not None and m.mood_score <= 3:
        now += " (her mood is low, so she is in no mood for flirting)"
    lines = []
    if not topic:
        if on and m.attraction >= 0.3:
            lines.append(f"Her way with flirting: {', '.join(on[:4])}; {now}.")
        return lines
    if on:
        lines.append(f"How {name} talks about attraction: {', '.join(on)}.")
    lines.append(f"How far: {now}. Vary it; she is not the same every time, and not every line is a flirt.")
    if off:
        lines.append(f"She never uses: {', '.join(off)}.")
    if d.toggles.get("private_vocabulary") and d.vocabulary:
        rng = random.Random(f"{name}:{salt}")
        take = 1 + int(d.sliders.get("vocabulary_variety", 50) // 34)
        samples = []
        needs = {"flirtatious": "flirtatious", "teasing": "teasing", "romantic": "romantic_expressions",
                 "suggestive": "suggestive"}
        for cat in ("terms_of_affection", "expressions", "flirtatious", "teasing", "romantic", "suggestive", "custom"):
            words = d.vocabulary.get(cat)
            if not words or (cat in needs and not d.toggles.get(needs[cat])) or (cat == "suggestive" and lic < 0.35):
                continue
            picked = rng.sample(words, min(len(words), take))
            samples.append(f"{CATEGORIES[cat]}: " + "; ".join(f'"{w}"' for w in picked))
        if samples:
            lines.append("Her own turns of phrase, as a guide to her voice, not lines to repeat word for word: "
                         + " | ".join(samples) + ".")
    for cat, lead in (("disliked", "She dislikes hearing or saying"), ("forbidden", "She never says")):
        if d.vocabulary.get(cat):
            lines.append(f"{lead}: " + "; ".join(f'"{w}"' for w in d.vocabulary[cat]) + ".")
    return lines


def to_json(d: Dialogue) -> dict[str, Any]:
    return {"enabled": d.enabled, "toggles": d.toggles, "sliders": d.sliders, "vocabulary": d.vocabulary,
            "sources": d.sources}


__all__ = ["TOGGLES", "SLIDERS", "CATEGORIES", "Dialogue", "Moment", "resolve", "generated", "validate",
           "style_lines", "license_for", "to_json", "schema"]
