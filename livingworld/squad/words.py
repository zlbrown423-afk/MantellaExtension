"""Reading squad words: the player asking people to come along, dismissing them, or setting the formation,
and an NPC's reply saying yes or no. Plain pattern rules, no model call.

Narrow on purpose, like ``guard/enlist_words.py``: a request is a plain ask ("Brand, come with me", "will you
walk with me?"), never a refusal or a condition ("don't follow me", "if you come with me"). An NPC's reply is
judged by its first sentence that says yes or no; anything else leaves the ask open.
"""
from __future__ import annotations

import re
from typing import Iterable

from livingworld.dialogue import joins

_NO_ASK = re.compile(r"\b(?:don'?t|do\s+not|never|stop|no\s+need|not|won'?t|without|unless|if|once|when|after|before)\b",
                     re.IGNORECASE)

_RECRUIT = re.compile(
    r"\b(?:follow\s+me|come\s+(?:with\s+me|along|on\s+with\s+me)|join\s+me|walk\s+with\s+me|patrol\s+with\s+me|"
    r"march\s+with\s+me|ride\s+with\s+me|stay\s+with\s+me|stick\s+with\s+me|fall\s+in|form\s+up|"
    r"i\s+(?:need|want)\s+(?:you|your\s+help)\s+(?:with\s+me|to\s+come|on\s+a\s+patrol)|"
    r"(?:be|come)\s+my\s+(?:escort|backup)|back\s+me\s+up\s+on)\b", re.IGNORECASE)

_DISMISS = re.compile(
    r"\b(?:stand\s+down|you\s*'?re\s+dismissed|you\s+are\s+dismissed|dismissed|fall\s+out|disband|"
    r"go\s+back\s+to\s+(?:your|the)\s+(?:post|duty|duties|patrol)|return\s+to\s+(?:your|the)\s+(?:post|duty|duties)|"
    r"back\s+to\s+(?:your\s+)?(?:post|duty)|you\s*'?re\s+free\s+to\s+go|you\s+can\s+go(?=\s+now|\s+back|\s*[.!,?]|\s*$)|"
    r"(?:stop|quit)\s+following\s+me|no\s+need\s+to\s+follow(?:\s+me)?\s+any\s*more)\b",
    re.IGNORECASE)

# (formation, pattern), first match wins: the most specific words first
_FORMATION = (
    ("autonomous", re.compile(r"\b(?:use\s+your\s+(?:own\s+)?judg(?:e)?ment|scout\s+ahead|fan\s+out|split\s+up|"
                              r"look\s+around\s+on\s+your\s+own|free\s+to\s+(?:investigate|scout|explore))\b", re.IGNORECASE)),
    ("patrol", re.compile(r"\b(?:patrol\s+formation|in\s+formation|march\s+in\s+(?:order|formation)|keep\s+formation|"
                          r"fall\s+in\s+line|(?:keep|in)\s+(?:a\s+)?column)\b", re.IGNORECASE)),
    ("close", re.compile(r"\b(?:stay\s+close|keep\s+close|close\s+ranks|tight\s+formation|stick\s+close|"
                         r"closer\s+to\s+me|don'?t\s+stray|stay\s+tight)\b", re.IGNORECASE)),
    ("loose", re.compile(r"\b(?:spread\s+out|give\s+me\s+(?:some\s+)?(?:room|space)|keep\s+your\s+distance|"
                         r"loose\s+formation|don'?t\s+crowd\s+me|back\s+off\s+a\s+(?:bit|little)|"
                         r"a\s+(?:bit|little)\s+(?:further|farther)\s+back)\b", re.IGNORECASE)),
)

_AGREE = re.compile(
    r"\b(?:aye|yes|yeah|yep|sure|of\s+course|very\s+well|lead\s+(?:the\s+way|on)|right\s+behind\s+you|"
    r"i\s*'?ll\s+(?:follow|come|go|walk|join|be\s+right|stay\s+with|fall\s+in)|i\s*'?m\s+(?:with\s+you|coming)|"
    r"count\s+me\s+in|consider\s+it\s+done|at\s+your\s+side|let\s*'?s\s+(?:go|move|march)|"
    r"you\s*'?ve\s+got\s+(?:it|me)|as\s+you\s+wish|lead\s+on|after\s+you|happy\s+to|gladly)\b", re.IGNORECASE)
_REFUSE = re.compile(
    r"\b(?:no|nope|can\s*'?t|cannot|won\s*'?t|will\s+not|i\s+must\s+(?:stay|remain)|my\s+post|on\s+duty|not\s+now|"
    r"i\s+have\s+(?:orders|my\s+duties)|sorry|afraid\s+not|i\s+(?:don'?t|do\s+not)\s+think\s+so|"
    r"out\s+of\s+the\s+question|rather\s+not|can'?t\s+leave)\b|\bn\s*'?t\b", re.IGNORECASE)


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'")


def asks_to_come(text: str) -> bool:
    """The player asks someone to come along (a question like 'will you come with me?' counts)."""
    text = _clean(text)
    for m in _RECRUIT.finditer(text):
        clause = re.split(r"[.!?;]", text[:m.start()])[-1] + re.split(r"[.!?;]", text[m.start():], maxsplit=1)[0]
        if not _NO_ASK.search(clause):
            return True
    return False


def dismisses(text: str) -> bool:
    text = _clean(text)
    m = _DISMISS.search(text)
    if not m:
        return False
    clause = re.split(r"[.!?;]", text[:m.start()])[-1]
    return not re.search(r"\b(?:don'?t|do\s+not|not|never|if|once)\b", clause, re.IGNORECASE)


def formation_in(text: str) -> str | None:
    text = _clean(text)
    for name, pattern in _FORMATION:
        if pattern.search(text):
            return name
    return None


def named_in(text: str, people: dict[int, list[str]]) -> list[int]:
    """The people this line names (by any name they go by, or by role for a generic NPC)."""
    return [i for i, names in people.items() if any(joins.named(text, n) for n in names if n)] if text else []


def targets(text: str, people: dict[int, list[str]]) -> list[int]:
    """Who the player's line is about: everyone present when they call a group ('everyone', 'you three'),
    the people named (by any name they go by, or by role for a generic NPC), or the only other person here.
    More than one unnamed person gives nobody, so no one is pulled in by mistake."""
    if not people or not text:
        return []
    if joins.calls_a_group(text):
        return list(people)
    named = named_in(text, people)
    if named:
        return named
    return list(people) if len(people) == 1 else []


def answer_in(text: str) -> bool | None:
    """Whether an NPC's reply says yes (True) or no (False) to coming along; None when it does not say."""
    for sentence in re.split(r"(?<=[.!?;])\s+", _clean(text)):
        yes, no = _AGREE.search(sentence), _REFUSE.search(sentence)
        if yes and not no:
            return True
        if no:
            return False
    return None


def formation_text(formation: str) -> str:
    return {
        "loose": "loose formation: keep a comfortable distance, do not crowd them, move naturally around obstacles",
        "close": "close formation: stay near them, suited to dangerous ground",
        "autonomous": "autonomous formation: use your own judgment, investigate nearby threats, and rejoin them afterwards",
        "patrol": "guard patrol formation: keep a disciplined line, stay alert to hostiles, and fight together",
    }.get(formation, "loose formation")


def member_lines(own: str, leader: str, formation: str, mates: Iterable[str], status: str) -> list[str]:
    """Plain facts for an NPC's prompt about being in the player's walking party."""
    if status == "asked":
        return [f"{leader} has just asked {own} to come along. {own} answers in their own way, yes or no; "
                f"agreeing means falling in beside {leader} until {leader} dismisses them."]
    mates = [m for m in mates if m]
    with_ = (" with " + (mates[0] if len(mates) == 1 else ", ".join(mates[:-1]) + " and " + mates[-1])) if mates else ""
    return [f"{own} is walking{with_} as part of {leader}'s party, in {formation_text(formation)}. "
            f"{own} stays with the party until {leader} dismisses them, and still fights as {own} always would."]
