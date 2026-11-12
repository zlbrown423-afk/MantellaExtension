"""Enlisting by word: a guard leader who says they take the player on counts as EnlistRecruit.

The model often says "I'll enlist you as a recruit... Here's your armour" without starting its reply with
``EnlistRecruit:`` (or calling the tool), so no service record is started and no uniform is handed over,
though the leader thinks it was. This keeper reads the NPC's spoken sentences, like ``dialogue/promises.py``,
and adds the EnlistRecruit action to the sentence that says it. The guard's rules still decide
(``Guard.action``): an NPC who cannot take people on, a player who already serves, has a bounty or was
dismissed lately is refused there, exactly as for the prefix or the tool call.

Narrow on purpose: only statements for now ("I'll enlist you", "welcome to the guard, recruit",
"you're one of us now", "here's your armour"), never a question, a refusal or a condition ("if you've
enlisted, you'll get your armour", "once you prove yourself"). At most once per reply.

Follows ``MANTELLA_LW_GUARD``; off on its own with ``MANTELLA_LW_ENLIST_WORDS=0``.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

ENV = "MANTELLA_LW_ENLIST_WORDS"
ENLIST = "mantella_lw_guard_enlist"

logger = logging.getLogger("livingworld.guard.enlist_words")


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


_LEAD = r"(?:\bi\s*'?\s*ll\b|\bi\s+will\b|\bi\s+hereby\b|\blet\s+me\b|\bi\s*'?\s*m\s+(?:going\s+to|gonna)\b|\bi\s+shall\b|\bwe\s*'?\s*ll\b)"
_GAP = r"[^.!?;]{0,30}?"
_PATTERNS = (
    re.compile(_LEAD + _GAP + r"\b(?:enlist|take\s+you\s+on|sign\s+you\s+(?:up|on)|swear\s+you\s+in|make\s+you\s+a\s+recruit"
               r"|accept\s+you|welcome\s+you)\b", re.IGNORECASE),
    re.compile(r"\bwelcome\b[^.!?;]{0,40}?\b(?:to|into|in)\s+the\s+(?:\w+\s+)?(?:guard|watch)\b", re.IGNORECASE),
    re.compile(r"\bwelcome\b[^.!?;]{0,40}?\bas\s+(?:a|our|the)\s+(?:new\s+)?recruit\b", re.IGNORECASE),
    re.compile(r"\byou\s*(?:'re|\s+are)\s+(?:now\s+)?(?:a\s+(?:new\s+)?recruit|one\s+of\s+us|part\s+of\s+the\s+(?:\w+\s+)?guard"
               r"|a\s+member\s+of\s+the\s+(?:\w+\s+)?guard)\b", re.IGNORECASE),
    re.compile(_LEAD + _GAP + r"\b(?:provide|give|hand|issue|get|fetch|equip|outfit|kit)\s+you\b[^.!?;]{0,25}?"
               r"\b(?:armou?r|uniform|gear|kit|equipment)\b", re.IGNORECASE),
    re.compile(_LEAD + _GAP + r"\b(?:suit|gear|kit)\s+you\s+up\b", re.IGNORECASE),
    re.compile(r"\b(?:enlist(?:ment|ed)?|recruit)\b[^.!?;]{0,40}?\bit\s+is\b", re.IGNORECASE),
    re.compile(r"\bhere\s*(?:'s|\s+is|\s+are)\s+(?:your|the)\s+(?:new\s+)?(?:armou?r|uniform|gear|kit|equipment)\b", re.IGNORECASE),
)
_NOT_NOW = re.compile(r"\b(?:not|never|no|if|once|unless|later|tomorrow|after|when|first|until|before|would|could|might"
                      r"|can't|cannot|won't)\b|n\s*'?\s*t\b", re.IGNORECASE)


def says_enlisted(text: str) -> bool:
    """This sentence takes the player on right now."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    if not text.strip() or text.rstrip().endswith("?"):
        return False
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            before = re.split(r"[.!?;]", text[:match.start()])[-1]
            clause = before + re.split(r"[.!?;]", text[match.start():], maxsplit=1)[0]
            if not _NOT_NOW.search(clause):
                return True
    return False


class EnlistWatcher:
    """One per reply: adds EnlistRecruit to the sentence in which a leader says they take the player on."""

    def __init__(self, actions: Iterable[Any], loaded: Iterable[str] = ()):
        self.offered = ENLIST in ({getattr(a, "identifier", None) for a in actions} | set(loaded))
        self.done = False

    def note_actions(self, actions: Iterable[Any]) -> None:
        if any(isinstance(a, dict) and a.get("identifier") == ENLIST for a in actions):
            self.done = True

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.note_actions(sentence_actions)
        if not self.offered or self.done or not says_enlisted(text):
            return []
        sentence_actions.append({"identifier": ENLIST, "arguments": {"source": speaker_name}})
        self.done = True
        logger.log(23, f"Living World guard: {speaker_name} said they take the player on; counting it as EnlistRecruit")
        return [ENLIST]
