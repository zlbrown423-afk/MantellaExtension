"""Fewer "How about you?" replies.

The dialogue model likes to end nearly every answer by handing the question back ("...and you?",
"How about you, friend?"). Once is friendly; every reply sounds like a script. Two layers:

* ``prompt_lines``: a standing rule in the prompt asking NPCs not to bounce questions back, with
  livelier things to do instead (ask something specific, react, share an opinion, or just stop).
* ``TurnBackTrimmer``: a cheap check on each spoken line. A bare hand-back at the end of a line
  ("What about you?", "And yourself?", "What of you, traveler?") never reaches the voice as is: the
  first one in a while is reworded into a warmer question ("How have you been keeping?"), and one
  the same NPC repeats within their next few replies (or in the same reply) is cut. A specific question
  ("How about you, ever been to Solitude?") is left alone, and a line is never cut to nothing when
  it is all the NPC has said so far this reply.

``MANTELLA_LW_TURN_BACK=0`` turns both off.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

TURN_BACK_ENV = "MANTELLA_LW_TURN_BACK"
_OFF = {"0", "false", "off", "no"}

GAP = 4   # a hand-back is allowed at most once in this many replies from the same NPC


def enabled() -> bool:
    return os.environ.get(TURN_BACK_ENV, "").strip().lower() not in _OFF


def prompt_lines(npc_names: Iterable[str], player_name: str) -> list[str]:
    """The standing rule, once per prompt."""
    names = [n for n in npc_names if n]
    who = names[0] if len(names) == 1 else "NPCs"
    return [
        f"Do not hand questions back to {player_name}. Never say \"What about you?\", \"How about you?\", "
        "\"And you?\", \"And yourself?\" or similar: said flatly it sounds cold and creepy.",
        f"Instead, {who} can react to what {player_name} said, give an opinion, add a detail of "
        "their own, ask one specific question about something just mentioned, or simply stop "
        "talking. Most replies need no question at all.",
        "If asking back really fits (once in a long while), make it warm and specific, e.g. "
        "\"Been on the road long?\" or \"How have you been keeping?\", never a bare \"And you?\".",
    ]


# ---- the line check ----
_VOCATIVE = (
    r"(?:\s*,?\s*(?i:(?:my\s+)?(?:friend|traveler|traveller|stranger|lad|lass|sera|serjo|outlander|"
    r"milord|milady|lord|lady|love|dear|kinsman|kinswoman|sister|brother|boy|girl|sir|madam)))"
    r"|(?:\s*,\s*[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)?)"
)
_TAIL_WORD = r"(?:\s*,?\s*(?i:then|though|now))"
_CORE = (
    r"(?i:"
    r"(?:(?:and|so|but|well)\s*,?\s*)?(?:how|what)\s+about\s+(?:you|yourself)"
    r"|(?:and|so)\s*,?\s*(?:you|yourself)"
    r"|(?:(?:and|so)\s*,?\s*)?what\s+of\s+(?:you|yourself)"
    r"|(?:and|so)\s+how\s+are\s+you"
    r")"
)
# the hand-back at the end of a line: after the start, a sentence end, a comma or a dash
_TRAILING = re.compile(
    r"(?P<lead>^|[.!?…]\s+|\s*[,;:—–-]\s*)"
    r"(?P<q>[\"'*]?" + _CORE + r"(?:" + _VOCATIVE + r")?(?:" + _TAIL_WORD + r")?\s*\?+[\"'*]?)\s*$"
)


def find(text: str) -> re.Match | None:
    """The hand-back question at the end of ``text``, or None."""
    return _TRAILING.search(text or "")


def strip(text: str) -> str:
    """``text`` without a trailing hand-back question. A clause cut off at a comma gets a full stop."""
    m = find(text)
    if m is None:
        return text
    kept = text[:m.start()].rstrip()
    if kept and m.group("lead").strip() and m.group("lead").strip()[0] in ",;:—–-":
        kept = kept.rstrip(",;:—–- ") + "."
    elif kept and m.group("lead").strip():
        kept = kept + m.group("lead").strip()   # keep the sentence end before it
    return kept


# warmer ways to ask back, used in place of the bare question when one is kept
REPHRASES = (
    "How have you been keeping?",
    "And how have things been for you?",
    "How's the road been treating you?",
    "What's been keeping you busy?",
    "How are you holding up?",
)


def rephrase(text: str, n: int = 0) -> str:
    """``text`` with its trailing bare hand-back swapped for a warmer question (``REPHRASES[n]``)."""
    m = find(text)
    if m is None:
        return text
    kept = strip(text)
    if kept and kept[-1] not in ".!?\u2026\"'*":
        kept += "."
    return (kept + " " if kept else "") + REPHRASES[n % len(REPHRASES)]


class TurnBackTrimmer:
    """Remembers, per speaker, which of their replies ended with a hand-back."""

    def __init__(self, gap: int = GAP) -> None:
        self.gap = gap
        self._reply = 0                          # reply counter (every NPC reply)
        self._seen: dict[str, int] = {}          # speaker -> the reply number last counted for them
        self._count: dict[str, int] = {}         # speaker -> how many replies they gave
        self._last: dict[str, int] = {}          # speaker -> their reply count when a hand-back was kept
        self._spoke: set[str] = set()            # speakers with words already this reply
        self.cut = 0
        self.reworded = 0

    def start_reply(self) -> None:
        self._reply += 1
        self._spoke = set()

    def reset(self) -> None:
        self.__init__(self.gap)

    def _reword(self, text: str) -> str:
        self.reworded += 1
        return rephrase(text, self.reworded - 1)

    def trim(self, speaker: str, text: str) -> str:
        """``text`` as it should be spoken by ``speaker``."""
        if self._seen.get(speaker) != self._reply:
            self._seen[speaker] = self._reply
            self._count[speaker] = self._count.get(speaker, 0) + 1
        mine = self._count[speaker]
        if find(text) is None:
            if text.strip():
                self._spoke.add(speaker)
            return text
        last = self._last.get(speaker)
        if last is None or mine - last >= self.gap:
            self._last[speaker] = mine           # this one is allowed, said warmly
            self._spoke.add(speaker)
            return self._reword(text)
        kept = strip(text)
        if not kept.strip() and speaker not in self._spoke:
            self._spoke.add(speaker)
            return self._reword(text)            # never leave the NPC saying nothing at all
        self.cut += 1
        if kept.strip():
            self._spoke.add(speaker)
        return kept
