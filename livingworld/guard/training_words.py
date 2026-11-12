"""Training by word: reporting back to the superior, and a fellow guard agreeing to a fist bout.

Two gaps the game showed (2026-11-12): a recruit drilled, came back to the Commander and said they were
reporting back, but the Commander never began a reply with ``ReportDuty:``, so the step never closed and there
was no way on. And the sparring step needs a fellow guard to start a brawl, which the model rarely does with
the ``Brawl:`` prefix either.

- ``says_reporting``: the PLAYER's line says they are reporting back or that the duty is done. The guard's
  rules then judge the report at once (``Guard.report_turn``) when the superior is in the conversation;
  a duty that is not done yet is not filed (no reprimand), the superior just says what is still missing.
- ``SparWatcher``: a fellow guard, told the player needs a fist bout, says "put up your fists" or similar:
  the Brawl action is added to that sentence (once per reply), as if the reply had begun with ``Brawl:``.
  This works even when Brawl is ticked in Mantella's Disabled Actions (its default): the bout is only asked of
  guards told about the training step.

Follows ``MANTELLA_LW_GUARD``; off on its own with ``MANTELLA_LW_TRAINING_WORDS=0``.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

ENV = "MANTELLA_LW_TRAINING_WORDS"
BRAWL = "mantella_npc_brawl"

logger = logging.getLogger("livingworld.guard.training_words")


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'")


_REPORT = (
    re.compile(r"\breport(?:ing)?\s+(?:back|in|for|on\s+my|to\s+you|my\s+(?:duty|progress))\b", re.IGNORECASE),
    re.compile(r"\b(?:here|come|came|back)\s+to\s+report\b", re.IGNORECASE),
    re.compile(r"\bi\s*'?\s*(?:ve|have)\s+(?:done|finished|completed)\s+(?:the|my|it|that)\b", re.IGNORECASE),
    re.compile(r"\b(?:drill|bout|spar(?:ring)?|fight|patrol|duty|shift|training|post|watch|task)(?:\s+is|\s+was|'s)\s+"
               r"(?:done|over|finished|complete|completed)\b", re.IGNORECASE),
    re.compile(r"\bi\s*'?\s*m\s+(?:back|done|finished)\b", re.IGNORECASE),
    re.compile(r"\bmission\s+accomplished\b|\ball\s+done\b", re.IGNORECASE),
)
_NOT_YET = re.compile(r"\b(?:not|never|haven't|havent|yet|when|if|should|how|where|what|can|could|may|will)\b|n\s*'?\s*t\b",
                      re.IGNORECASE)


def says_reporting(text: str) -> bool:
    """The player says they are back to report, or that their duty is done (not a question about it)."""
    text = _clean(text)
    if not text.strip():
        return False
    for pattern in _REPORT:
        for match in pattern.finditer(text):
            before = re.split(r"[.!?;]", text[:match.start()])[-1]
            rest = re.split(r"([.!?;])", text[match.start():], maxsplit=1)
            clause = before + rest[0]
            if len(rest) > 1 and rest[1] == "?":
                continue
            if not _NOT_YET.search(clause):
                return True
    return False


_LEAD = r"(?:\blet\s*'?\s*s\b|\bi\s*'?\s*ll\b|\bi\s+will\b|\bwe\s*'?\s*ll\b|\bcome\s+on\b|\balright\b|\bvery\s+well\b|\bfine\b)"
_GAP = r"[^.!?;]{0,30}?"
_SPAR = (
    re.compile(r"\b(?:put|get|raise|keep)\s+(?:up\s+)?your\s+(?:fists|dukes|hands)\s*(?:up)?\b", re.IGNORECASE),
    re.compile(r"\bfists\s+up\b|\bhave\s+at\s+(?:you|thee)\b|\bshow\s+me\s+what\s+you\s*'?\s*(?:ve\s+)?got\b"
               r"|\byou\s*'?\s*re\s+on\b|\ba\s+bout\s+it\s+is\b|\bspar(?:ring)?\s+it\s+is\b", re.IGNORECASE),
    re.compile(_LEAD + _GAP + r"\b(?:spar|brawl|scrap|fight|go\s+a\s+(?:round|bout)|have\s+a\s+(?:round|bout|go))\b",
               re.IGNORECASE),
)
_NO_SPAR = re.compile(r"\b(?:not|never|no|if|once|unless|later|tomorrow|after|when|until|before|would|could|might"
                      r"|can't|cannot|won't|real|steel|blade|sword|kill|death)\b|n\s*'?\s*t\b", re.IGNORECASE)


def says_spar(text: str) -> bool:
    """A fellow guard agrees to the bout, now: "Put up your fists", "Let's spar", "You're on"."""
    text = _clean(text)
    if not text.strip() or text.rstrip().endswith("?"):
        return False
    for pattern in _SPAR:
        for match in pattern.finditer(text):
            before = re.split(r"[.!?;]", text[:match.start()])[-1]
            clause = before + re.split(r"[.!?;]", text[match.start():], maxsplit=1)[0]
            if not _NO_SPAR.search(clause):
                return True
    return False


class SparWatcher:
    """One per reply: adds Brawl to the sentence in which a fellow guard (named in ``partners``) agrees to spar."""

    def __init__(self, partners: Iterable[str]):
        self.partners = {p.lower() for p in partners if p}
        self.done = False

    def note_actions(self, actions: Iterable[Any]) -> None:
        if any(isinstance(a, dict) and a.get("identifier") == BRAWL for a in actions):
            self.done = True

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.note_actions(sentence_actions)
        if (self.done or (speaker_name or "").lower() not in self.partners
                or not says_spar(text)):
            return []
        sentence_actions.append({"identifier": BRAWL, "arguments": {"source": [speaker_name]}})
        self.done = True
        logger.log(23, f"Living World guard: {speaker_name} agreed to a training bout; counting it as Brawl")
        return [BRAWL]
