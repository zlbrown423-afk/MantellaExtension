"""Cavalry orders by voice: the player says "Mount up!" in a conversation and their riders do it.

The Whiterun Cavalry Training Grounds mod (WhiterunCavalryTraining.esp, a separate plugin) gives the player two
powers, Cavalry Orders and the Assembly Horn, each with a menu of orders. This module lets the player give the
same orders out loud while a Mantella conversation is open (Mantella only listens during a conversation):

=================  ====  =====================================================================
order              code  what the player says (examples)
=================  ====  =====================================================================
mount up           1     "mount up", "saddle up", "to horse", "get on your horses"
dismount           2     "dismount", "off your horses", "get down off your horses"
fall in (column)   3     "fall in", "fall in line", "form a column", "single file"
line abreast       4     "line abreast", "form a line", "form up abreast"
at ease            5     "at ease", "break formation", "fall out"
rally to me        6     "rally to me", "rally on me", "riders, to me", "form on me"
rally at the hall  7     "rally at the hall", "back to Greymarch", "wait at the stables"
=================  ====  =====================================================================

Only the player's own words count, never an NPC's. Deliberately narrow, so a passing remark never moves the
riders: a question ("Should we mount up?"), a negation ("Don't dismount yet"), an order for later ("When we
reach the gate, dismount") or reported speech ("I told them to fall in") does nothing.

A line can carry up to three orders, carried out in the order they are spoken ("Mount up and fall in" = 1 then
3). They go to the game with the next NPC line as one number, one digit per order (13), under
``mantella_lw_cavalry_order``. The game side (MantellaRepository.LwCavalryOrder) hands the number to the
cavalry plugin; without that plugin nothing happens.

On with the Living World unless ``MANTELLA_LW_CAVALRY_VOICE=0``.
"""
from __future__ import annotations

import os
import re

ENV = "MANTELLA_LW_CAVALRY_VOICE"
KEY_ORDER = "mantella_lw_cavalry_order"
_OFF = {"0", "false", "no", "off"}

MOUNT, DISMOUNT, COLUMN, LINE, EASE, RALLY_ME, RALLY_HALL = 1, 2, 3, 4, 5, 6, 7
MAX_ORDERS = 3

NAMES = {
    MOUNT: "mount up",
    DISMOUNT: "dismount",
    COLUMN: "fall in (column)",
    LINE: "line abreast",
    EASE: "at ease",
    RALLY_ME: "rally to me",
    RALLY_HALL: "rally at the hall",
}

_HORSES = r"(?:horses?|mounts?|steeds?|saddles?)"
_PLACE = r"(?:the\s+)?(?:(?:guild\s+|wardens'?\s+)?hall|stables?|yard|paddock|barracks|greymarch)"

# Checked in this order: the longer phrase wins where two overlap ("fall in line abreast" is a line).
_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (RALLY_HALL, re.compile(
        r"\b(?:rally|muster|regroup|gather|assemble|wait|meet)\s+(?:back\s+)?(?:at|in|by)\s+" + _PLACE + r"\b"
        r"|\b(?:back|return|head|ride|go)\s+(?:back\s+)?to\s+" + _PLACE + r"\b", re.IGNORECASE)),
    (RALLY_ME, re.compile(
        r"\b(?:rally|form|regroup|muster|gather)\s+(?:to|on|around|round)\s+me\b"
        r"|\briders?\s*,?\s+to\s+me\b|\bto\s+me\s*,?\s+riders?\b|\bwardens?\s*,?\s+to\s+me\b", re.IGNORECASE)),
    (LINE, re.compile(
        r"\bline\s+abreast\b|\babreast\b|\bform\s+(?:up\s+)?(?:a\s+|in\s+(?:a\s+)?)?line\b(?!\s+(?:behind|up\s+behind))"
        r"|\bline\s+formation\b|\bride\s+(?:in\s+(?:a\s+)?)?line\b", re.IGNORECASE)),
    (COLUMN, re.compile(
        r"\bfall\s+in\b(?!\s+(?:a\s+)?line\s+abreast|\s+(?:love|the|a|an|to|into|with|there|here)\b)|\bform\s+(?:up\s+)?(?:a\s+|in\s+(?:a\s+)?)?column\b|\bcolumn\s+formation\b|\bin\s+column\b"
        r"|\bsingle\s+file\b|\bline\s+up\s+behind\s+me\b", re.IGNORECASE)),
    (EASE, re.compile(r"\bat\s+ease\b|\bbreak\s+(?:formation|ranks)\b|\bfall\s+out\b", re.IGNORECASE)),
    (DISMOUNT, re.compile(
        r"\bdismount\b|\b(?:get\s+)?(?:off|down\s+(?:off|from))\s+(?:your|the|those)\s+" + _HORSES + r"\b", re.IGNORECASE)),
    (MOUNT, re.compile(
        r"\bmount\s+up\b|\bsaddle\s+up\b|\bto\s+horse\b|\bmount\s+(?:your|the)\s+" + _HORSES + r"\b"
        r"|\b(?:get|climb|hop)\s+(?:up\s+)?(?:on|onto|in)\s+(?:your|the)\s+" + _HORSES + r"\b"
        r"|\beveryone\s*,?\s+mount\b|\bmount\s*!", re.IGNORECASE)),
)

# Words before the order, in the same clause, that make it not an order for right now.
_NOT_NOW = re.compile(
    r"\b(?:don'?t|do\s+not|never|not|no\s+need|won'?t|didn'?t|can'?t|shouldn'?t|wouldn'?t|without|unless|if|once|when"
    r"|whenever|after|before|until|till|later|tomorrow|told|said|asked|ordered|remember|meant|means"
    r"|how\s+(?:do|to)|what\s+(?:does|do)|should|shall|could|would|might|did|does)\b", re.IGNORECASE)
# The same, after the order: "dismount when we get there", "fall in later".
_LATER = re.compile(r"\b(?:when|once|after|later|until|till|tomorrow|if|unless|before)\b", re.IGNORECASE)
_QUESTION_START = re.compile(
    r"^\s*(?:should|shall|could|would|can|do|does|did|are|is|will|why|what|how|when|where|who)\b", re.IGNORECASE)


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def _clauses(text: str) -> list[str]:
    """Sentences and the parts of a sentence joined by 'and' / 'then'."""
    parts: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if sentence.endswith("?") or _QUESTION_START.match(sentence):
            continue
        for clause in re.split(r",?\s+(?:and\s+then|and|then)\s+|\s*,\s*(?:then|now)\s+", sentence, flags=re.IGNORECASE):
            if clause.strip():
                parts.append(clause.strip())
    return parts


def orders_in(text: str) -> list[int]:
    """The cavalry orders the player gives in this line, in the order spoken (at most three, no repeats)."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    if not text.strip():
        return []
    found: list[int] = []
    for clause in _clauses(text):
        hits: list[tuple[int, int, int]] = []   # (start, end, code)
        for code, pattern in _PATTERNS:
            for match in pattern.finditer(clause):
                if any(match.start() < end and start < match.end() for start, end, _ in hits):
                    continue   # part of a longer order already found ("fall in line abreast")
                hits.append((match.start(), match.end(), code))
        for start, end, code in sorted(hits):
            if _NOT_NOW.search(clause[:start]) or _LATER.search(clause[end:]):
                continue
            if code not in found:
                found.append(code)
            if len(found) >= MAX_ORDERS:
                return found
    return found


def wire(orders: list[int]) -> int:
    """One number for the game, one digit per order: [1, 3] -> 13."""
    value = 0
    for code in orders[:MAX_ORDERS]:
        value = value * 10 + int(code)
    return value


def note(orders: list[int]) -> str:
    """A line for the dialogue model, so the NPC reacts to the order instead of ignoring it."""
    said = ", then ".join(NAMES[c] for c in orders)
    return (f"The player just gave their riders the order: {said}. The player's followers carry it out; "
            "a follower who is talking with the player may answer briefly, as a rider taking an order would.")


class CavalryVoice:
    """This conversation's spoken cavalry orders, waiting to go to the game with the next NPC line."""

    def __init__(self) -> None:
        self._last_text: str | None = None
        self._pending: list[int] = []
        self._notes: list[str] = []

    def heard(self, text: str) -> list[int]:
        """Read a player line once (it may be offered twice: for the model's notes and as the player's line)."""
        text = (text or "").strip()
        if not text or text == self._last_text:
            return []
        self._last_text = text
        orders = orders_in(text)
        if orders:
            self._pending = (self._pending + [o for o in orders if o not in self._pending])[:MAX_ORDERS]
            self._notes.append(note(orders))
        return orders

    def take_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    def take_keys(self) -> dict[str, int]:
        if not self._pending:
            return {}
        value, self._pending = wire(self._pending), []
        return {KEY_ORDER: value}
