"""Agreeing to work in words: the player offers to help and the NPC accepts, but nobody calls AgreeTask.

In play the dialogue model often settles a job in speech only ("I'll help you if you need it." /
"Thank you! That would be a great help with the harvest."), so the task never moved to accepted. This
watches for that, only for a task the NPC's prompt offered to the player in this conversation:

- the player offers help ("I'll help you", "I can lend a hand", "count me in"), or says yes to the NPC's
  question about the work ("Would you help with the harvest?" / "Of course."), and
- the NPC's next line takes it up (anything but a refusal or a question back).

Rules only, no LLM. Off with the tasks (``MANTELLA_LW_TASKS=0``) or on its own with
``MANTELLA_LW_TASK_WORDS=0``.
"""
from __future__ import annotations

import os
import re

WORDS_ENV = "MANTELLA_LW_TASK_WORDS"
_OFF = {"0", "false", "no", "off"}


def words_enabled() -> bool:
    return os.environ.get(WORDS_ENV, "").strip().lower() not in _OFF


_OFFER = re.compile(
    r"\b(?:i\s*'?\s*ll|i\s+will|i\s+can|i\s+could|i\s*'?\s*d\s+be\s+(?:happy|glad)\s+to|happy\s+to|glad\s+to"
    r"|let\s+me|i\s*'?\s*m\s+(?:happy|glad|willing)\s+to|i\s+don\s*'?\s*t\s+mind)\s+(?:\w+\s+){0,2}?"
    r"(?:help|lend\s+(?:you\s+)?a\s+hand|give\s+(?:you\s+)?a\s+hand|do\s+it|do\s+that|pitch\s+in|work|harvest|pick)\b"
    r"|\bcount\s+me\s+in\b|\bi\s*'?\s*m\s+in\b|\bneed\s+(?:a\s+hand|help)\s*\?|\bwant\s+(?:some\s+)?help\s*\?"
    r"|\bhow\s+can\s+i\s+help\b|\bput\s+me\s+to\s+work\b",
    re.IGNORECASE)
_NOT = re.compile(r"\b(?:can\s*'?\s*t|cannot|won\s*'?\s*t|not|never|don\s*'?\s*t\s+(?:want|have\s+time)|no\s+time|busy"
                  r"|rather\s+not|maybe\s+later|another\s+time)\b", re.IGNORECASE)
_YES = re.compile(r"^\W*(?:yes|yeah|yep|aye|sure|of\s+course|ok|okay|alright|all\s+right|certainly|absolutely"
                  r"|gladly|happy\s+to|why\s+not|let\s*'?\s*s\s+(?:do|go))\b", re.IGNORECASE)
_WORK_ASK = re.compile(r"\b(?:help|hand|work|harvest|chores?|job|task|pick|gather|bring|firewood|crops?)\b", re.IGNORECASE)
_REFUSE = re.compile(r"\b(?:no\s+thanks|no\s+thank\s+you|not\s+(?:now|today|necessary|needed)|i\s+can\s+manage"
                     r"|i\s*'?\s*ll\s+manage|don\s*'?\s*t\s+need|no\s+need\s+(?:for|to\s+help)|maybe\s+(?:later|another))\b",
                     re.IGNORECASE)


def player_offers(text: str, npc_last: str | None = None) -> bool:
    """The player's line offers help, or says yes to the NPC asking for help with the work."""
    text = (text or "").replace("’", "'")
    for m in _OFFER.finditer(text):
        clause = re.split(r"[.!?;,]|\bbut\b", text[:m.end()])[-1]   # "not my favourite, but I'll help" is an offer
        if not _NOT.search(clause):
            return True
    asked = (npc_last or "").rstrip().endswith("?") and bool(_WORK_ASK.search(npc_last or ""))
    return asked and bool(_YES.search(text)) and not _NOT.search(text)


def npc_takes_it_up(text: str) -> bool:
    """The NPC's answer to the offer accepts it (not a refusal, not a question back)."""
    text = (text or "").replace("’", "'").strip()
    return bool(text) and not text.endswith("?") and not _REFUSE.search(text)
