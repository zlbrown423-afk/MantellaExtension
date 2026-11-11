"""Only pull people into a conversation when the player asked for them.

Mantella's ``AddToConversation`` action lets the AI add anyone nearby. A separate tool-calling model
(Mantella's "custom function LLM") sees the "Characters nearby" note on the very first line of every
conversation and adds them all, guards, shopkeepers and the odd cow included, so a quiet word with one
NPC turns into a crowd. This keeps the call only for the people the player's own last line names
("Gerdur, come here", "guard!"), or everyone it asks for when the player calls a group over
("everyone, listen", "join us"). The NPC can still call someone over by name in what they say; the
player then decides.

On with the Living World unless ``MANTELLA_LW_ASKED_JOINS=0``.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

JOINS_ENV = "MANTELLA_LW_ASKED_JOINS"
_OFF = {"0", "false", "no", "off"}

ADD_ACTION = "mantella_npc_addtoconversation"

# the player calls a whole group over: keep every name the AI picked
_GROUP = re.compile(
    r"\b(?:every(?:one|body)|all\s+of\s+you|you\s+all|y'?all|you\s+(?:two|three|lot|guys)|both\s+of\s+you"
    r"|join\s+(?:us|in|the\s+conversation))\b",
    re.IGNORECASE)

# generic NPC names ("Whiterun Guard", "Imperial Soldier"): the role word is what a player says
_ROLES = {"guard", "soldier", "guardsman", "housecarl", "bandit", "beggar", "farmer", "courier", "hunter",
          "miner", "merchant", "priest", "priestess", "mage", "thug", "legionnaire", "stormcloak", "citizen",
          "woman", "man", "child", "boy", "girl", "servant", "maid", "cook", "bard", "patron", "worker"}


def joins_enabled() -> bool:
    return os.environ.get(JOINS_ENV, "").strip().lower() not in _OFF


def _said(text: str, word: str) -> bool:
    return len(word) >= 3 and re.search(rf"\b{re.escape(word)}s?\b", text, re.IGNORECASE) is not None


def named(text: str, name: str) -> bool:
    """Whether ``text`` names ``name``: in full, by first name, or by role for a generic NPC."""
    if not text or not name:
        return False
    if _said(text, name):
        return True
    words = name.split()
    if len(words) > 1 and words[-1].lower() in _ROLES:
        return _said(text, words[-1])   # "Whiterun Guard": "guard", never "Whiterun"
    return _said(text, words[0])


def calls_a_group(text: str) -> bool:
    return bool(text) and _GROUP.search(text) is not None


def keep_asked(actions: Iterable[dict[str, Any]], player_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """``actions`` with AddToConversation cut down to the people ``player_text`` asked for.

    Returns (actions to keep, names dropped). Other actions pass through unchanged."""
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    group = calls_a_group(player_text)
    for action in actions:
        if not isinstance(action, dict) or action.get("identifier") != ADD_ACTION or group:
            kept.append(action)
            continue
        args = action.get("arguments") or {}
        source = args.get("source")
        names = source if isinstance(source, list) else [source] if source else []
        asked = [n for n in names if named(player_text, str(n))]
        dropped += [str(n) for n in names if n not in asked]
        if asked:
            kept.append({**action, "arguments": {**args, "source": asked if isinstance(source, list) else asked[0]}})
    return kept, dropped
