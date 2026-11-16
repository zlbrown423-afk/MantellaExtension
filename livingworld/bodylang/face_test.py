"""Test command for NPC facial expressions, said (or typed) in a conversation.

* "debug face happy"          - the NPC who answers holds a happy face for 5 seconds
* "debug face angry on Hod"   - the same for Hod (named anywhere in the line), when he is in the conversation
* "debug face sad 50"         - at 50% strength (default 80)

The words "debug" (or "test"), "face" and a mood must all be in the line, so normal speech never triggers it.
The game uses Skyrim's own ``Actor.SetExpressionOverride`` with the "Mood" faces (7-14; the "Dialogue" and
"Combat" ones barely work on women) and always clears it after the hold, at the end of the conversation,
on a load, or when an OStim scene has the NPC. Nothing reaches the AI beyond the line itself.

Off: ``MANTELLA_LW_FACE_TEST=0`` (or body language off).
"""
from __future__ import annotations

import os
import re

ENV = "MANTELLA_LW_FACE_TEST"
KEY_FACE = "mantella_lw_face"     # sent with an NPC line: 'mood|strength|seconds|Name' (no Name: the speaker)
HOLD_SECONDS = 5.0
DEFAULT_STRENGTH = 80

# Skyrim's expression numbers (Actor.SetExpressionOverride): the "Mood" set
MOODS = {
    "neutral": 7, "calm": 7,
    "angry": 8, "anger": 8, "mad": 8,
    "afraid": 9, "fear": 9, "scared": 9, "frightened": 9,
    "happy": 10, "smile": 10, "smiling": 10,
    "sad": 11, "unhappy": 11,
    "surprised": 12, "surprise": 12, "shocked": 12,
    "puzzled": 13, "confused": 13, "thinking": 13,
    "disgusted": 14, "disgust": 14,
}


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in ("0", "false", "no", "off")


def parse(text: str) -> tuple[int, int] | None:
    """(mood number, strength 0-100) or None."""
    words = re.findall(r"[a-z]+|\d+", (text or "").lower())
    if not ({"debug", "test"} & set(words)) or "face" not in words:
        return None
    mood = next((MOODS[w] for w in words if w in MOODS), None)
    if mood is None:
        return None
    numbers = [int(w) for w in words if w.isdigit()]
    strength = max(0, min(100, numbers[0])) if numbers else DEFAULT_STRENGTH
    return mood, strength


def target(text: str, names: list[str]) -> str:
    """The NPC named in the line, or '' for whoever answers."""
    lowered = (text or "").lower()
    for name in names:
        if name and re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return name
    return ""


def wire(name: str, mood: int, strength: int, seconds: float = HOLD_SECONDS) -> str:
    return f"{mood}|{strength}|{seconds:g}|{name}"
