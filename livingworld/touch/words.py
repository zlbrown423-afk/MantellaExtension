"""Touch consent from words: the player asks, the NPC says yes (or takes it back), and no tool was called.

The dialogue model often answers "Of course, come here" to "can I hug you?" without calling
``AllowTouch``, so nothing would change in the game and the hug would still read as a grab. This watcher
sits in the reply parser chain (``dialogue/promises.py`` runs it with the other keepers) and reads each
spoken sentence of the NPC:

- The player's line asked for one or more kinds of touch ("can I hug you?", "may I hold your hand?",
  "let me rub your back") and the NPC's sentence agrees ("Yes.", "Of course, come here.") without any
  "no", "not", "don't": ``AllowTouch`` for those kinds is added to the sentence.
- The NPC offers it themselves ("Come here, give me a hug", "Take my hand"): ``AllowTouch`` for that kind.
- The NPC takes it back ("Stop that.", "Hands off!", "That's enough."): ``StopTouch``.

Deliberately narrow: only the NPC's own speech, a question never agrees, anything with a negation does
nothing, and each action at most once per reply. When in doubt nothing happens and the touch stays an
ordinary grab, which is the safe side.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from livingworld import touch as T

logger = logging.getLogger("livingworld.touch")

_V = r"(?:\w+\s+){0,3}?"   # a few words between: "hold your hand", "give you a big hug"

# What the player asked for, kind by kind.
_ASKED = {
    "hug": re.compile(r"\b(?:hugs?|hugging|embrace|cuddle|hold\s+you|in\s+my\s+arms)\b", re.I),
    "hands": re.compile(r"\b(?:hold|take|holding|grab)\s+" + _V + r"hands?\b|\bhand\s+in\s+hand\b", re.I),
    "back": re.compile(r"\b(?:rub|massage|scratch|stroke)\w*\s+" + _V + r"(?:back|shoulders)\b|\bback\s*rub\b", re.I),
    "neck": re.compile(r"\b(?:touch|kiss|rub|massage|stroke|caress)\w*\s+" + _V + r"neck\b", re.I),
    "face": re.compile(r"\b(?:touch|stroke|cup|caress|hold)\w*\s+" + _V + r"(?:face|cheeks?)\b", re.I),
    "butt": re.compile(r"\b(?:grab|touch|squeeze|pat|feel|slap|hold|cup)\w*\s+" + _V
                       + r"(?:butt|bum|backside|arse|ass|behind|rear|bottom|buttocks)\b", re.I),
    "chest": re.compile(r"\b(?:grab|touch|squeeze|feel|fondle|hold|cup|caress)\w*\s+" + _V
                        + r"(?:breasts?|boobs?|tits|chest|bosom)\b", re.I),
    "anywhere": re.compile(r"\btouch\s+you\s+(?:anywhere|everywhere|all\s+over)\b", re.I),
}

# The player's line asks (rather than tells a story about a hug).
_ASKING = re.compile(r"\?|\b(?:can|may|could|might)\s+i\b|\blet\s+me\b|\bi\s+(?:want|wanna|would\s+like|'d\s+like|'d\s+love)"
                     r"|\bwould\s+you\b|\bdo\s+you\s+(?:mind|want)\b|\bhow\s+about\b|\bgive\s+(?:me|us)\b|\bcome\s+here\b"
                     r"|\bmind\s+if\b|\bis\s+it\s+(?:ok|okay|alright|all\s+right)\b", re.I)

# The NPC agrees, at the start of the sentence.
_YES = re.compile(r"^\W*(?:yes|yeah|yea|aye|of\s+course|sure|alright|all\s+right|okay|ok|please|go\s+ahead|come\s+here"
                  r"|very\s+well|gladly|certainly|why\s+not|mm+|i'?d\s+(?:like|love)\s+that|i\s+would\s+(?:like|love)\s+that"
                  r"|i'?d\s+be\s+glad|by\s+all\s+means|absolutely|go\s+on|you\s+may|you\s+can|fine|i\s+suppose)\b", re.I)

# The NPC offers it unasked.
_OFFER = {
    "hug": re.compile(r"\b(?:give\s+(?:me|us)\s+a\s+(?:\w+\s+)?hug|come\s+here\s*(?:,|and)?\s*(?:you)?\s*[^.!?]{0,20}\bhug"
                      r"|let\s+me\s+hug\s+you|hug\s+me|bring\s+it\s+in|into\s+my\s+arms)\b", re.I),
    "hands": re.compile(r"\b(?:take|hold)\s+my\s+hand\b|\bgive\s+me\s+your\s+hand\b", re.I),
}

_NEGATION = re.compile(r"\b(?:no|not|never|don'?t|do\s+not|won'?t|can'?t|cannot|shouldn'?t|rather\s+not|nor|stop"
                       r"|later|another\s+time|n't)\b|n't\b", re.I)

# The NPC takes it back.
_STOP = re.compile(r"\b(?:stop\s+(?:that|it|touching|now)|get\s+your\s+hands\s+off|hands\s+off|don'?t\s+touch\s+me"
                   r"|do\s+not\s+touch\s+me|let\s+(?:go\s+of\s+)?me\s+go|let\s+go\s+of\s+me|unhand\s+me|that'?s\s+enough"
                   r"|enough\s+of\s+that|back\s+off|keep\s+your\s+hands\s+to\s+yourself|too\s+far|get\s+off\s+me)\b", re.I)


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").strip()


def asked_kinds(player_said: str | None) -> list[str]:
    """The kinds of touch the player's line asks the NPC for."""
    text = _clean(player_said or "")
    if not text or not _ASKING.search(text):
        return []
    return [k for k in T.KINDS if _ASKED[k].search(text)]


def agrees(text: str) -> bool:
    """The sentence is a plain yes."""
    text = _clean(text)
    if not text or text.endswith("?"):
        return False
    return bool(_YES.search(text)) and not _NEGATION.search(text)


def offered_kinds(text: str) -> list[str]:
    text = _clean(text)
    if not text or text.endswith("?") or _NEGATION.search(text):
        return []
    return [k for k, pattern in _OFFER.items() if pattern.search(text)]


def withdraws(text: str) -> bool:
    text = _clean(text)
    return bool(text) and bool(_STOP.search(text))


class TouchWatcher:
    """One per reply, the same shape as ``promises.PromiseKeeper``."""

    def __init__(self, player_said: str | None, offered: Iterable[str]):
        offered = set(offered)
        self._can_allow = T.ALLOW in offered
        self._can_withdraw = T.WITHDRAW in offered
        self._asked = asked_kinds(player_said)
        self._done: set[str] = set()

    def note_actions(self, actions: Iterable[Any]) -> None:
        for action in actions:
            identifier = action.get("identifier") if isinstance(action, dict) else None
            if identifier in (T.ALLOW, T.WITHDRAW):
                self._done.add(identifier)

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.note_actions(sentence_actions)
        if self._done:
            return []
        if self._can_withdraw and withdraws(text):
            sentence_actions.append({"identifier": T.WITHDRAW, "arguments": {"source": speaker_name}})
            self._done.add(T.WITHDRAW)
            logger.log(23, f"Living World touch: {speaker_name} said to stop; taking the agreement back")
            return [T.WITHDRAW]
        if not self._can_allow:
            return []
        kinds = self._asked if (self._asked and agrees(text)) else offered_kinds(text)
        if not kinds:
            return []
        sentence_actions.append({"identifier": T.ALLOW, "arguments": {"source": speaker_name, "touch": kinds,
                                                                     "via": "words"}})
        self._done.add(T.ALLOW)
        logger.log(23, f"Living World touch: {speaker_name} agreed in words to: {', '.join(kinds)}")
        return [T.ALLOW]
