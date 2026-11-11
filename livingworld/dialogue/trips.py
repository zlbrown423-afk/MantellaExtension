"""Trips: an NPC who agrees to go somewhere with the player actually sets off, after the player confirms.

The LLM often answers "let's head to Battle-Born Farm" in words only, without calling Mantella's
``LeadTo`` / ``TravelTo`` tool, so nothing moves in the game and the NPC keeps agreeing in a loop.
This module watches for that and asks the player first, because the place name usually came through
speech-to-text and may be wrong:

1. The NPC's line agrees to set off ("let's head to the farm", "lead the way", "follow me") and the
   reply has not already started a trip.
2. The place comes from that line, or from what the player just asked for ("let's go to battleborn
   farm"). Names are matched against the places ``LeadTo`` knows (``travel_places.json``, the list in
   Mantella-Spell's ``MantellaLocationUtils.psc``), loosely, so "Brees home" still finds Breezehome.
3. The game shows a notification: "Go with Alfhild to Battle-Born Farm? Say yes or no." When the
   name could be one of several places, it shows numbered choices ("say one, two or three").
4. On "yes" (or "one", "two", a place name), the NPC's next line carries ``LeadTo``: the NPC walks and
   the player follows. Where ``LeadTo`` is not offered, ``TravelTo`` with ``leave_now``. "No" drops it.

Nothing is ever started without the player's yes. A place the player turned down is not asked about
again in the same conversation, and a question the player ignores lapses after two lines.

On with the Living World unless ``MANTELLA_LW_TRIPS=0``.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TRIPS_ENV = "MANTELLA_LW_TRIPS"
_OFF = {"0", "false", "no", "off"}

LEAD_TO = "mantella_npc_leadto"
TRAVEL_TO = "mantella_npc_travelto"
FOLLOW = "mantella_npc_follow"
_TRIP_ACTIONS = {LEAD_TO, TRAVEL_TO}
_STARTED = _TRIP_ACTIONS | {FOLLOW}   # the reply already does something about moving: never ask on top

MAX_CHOICES = 3
PATIENCE = 2           # player lines a question waits for an answer before it lapses
RECENT_LINES = 4       # how far back a place the player asked for still counts

logger = logging.getLogger("livingworld.trips")

_PLACES_FILE = Path(__file__).resolve().parent / "travel_places.json"


def trips_enabled() -> bool:
    return os.environ.get(TRIPS_ENV, "").strip().lower() not in _OFF


# ---------------------------------------------------------------- places

def _norm(text: str) -> str:
    text = (text or "").lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"'s\b", "s", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"^the ", "", _norm(text)).replace(" ", "")


@dataclass(frozen=True)
class Place:
    name: str
    form_id: str


_places_cache: list[Place] | None = None


def places() -> list[Place]:
    global _places_cache
    if _places_cache is None:
        rows = json.loads(_PLACES_FILE.read_text(encoding="utf-8"))
        _places_cache = [Place(name, form_id) for name, form_id in rows]
    return _places_cache


# Spoken words that never name a place on their own, so a loose match on them is noise.
_COMMON = {"farm", "home", "house", "inn", "shack", "camp", "mine", "cave", "tower", "mill", "fort", "temple",
           "there", "here", "town", "city", "village", "market", "shop", "store", "hall", "keep", "palace"}


def find_places(text: str, exclude: Iterable[str] = (), limit: int = MAX_CHOICES) -> list[Place]:
    """Places named in ``text``, best first. An exact name wins alone; otherwise the close sound-alikes,
    so a misheard name offers a few choices. ``exclude``: place names to leave out (where we are now)."""
    words = _norm(text).split()
    if not words:
        return []
    skip = {_compact(e) for e in exclude if e}
    grams: list[str] = []
    for n in range(1, 6):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] == "the":
                gram = gram[1:]
            g = "".join(gram)
            if len(g) >= 5 and " ".join(gram) not in _COMMON:
                grams.append(g)
    if not grams:
        return []
    gram_set = set(grams)
    exact = [p for p in places() if _compact(p.name) in gram_set and _compact(p.name) not in skip]
    if exact:
        exact.sort(key=lambda p: -len(p.name))   # "Battle-Born Farm" over a shorter name inside it
        return _dedupe(exact)[:1]
    scored: list[tuple[float, Place]] = []
    for p in places():
        c = _compact(p.name)
        if c in skip or len(c) < 5:
            continue
        near = [g for g in gram_set if abs(len(g) - len(c)) <= max(3, len(c) // 3)]
        best = max((difflib.SequenceMatcher(None, g, c).ratio() for g in near), default=0.0)
        if best >= 0.82:
            scored.append((best, p))
    scored.sort(key=lambda t: -t[0])
    top = scored[0][0] if scored else 0.0
    found = [p for s, p in scored if s >= top - 0.08]
    # Misheard names often keep one telling word ("the battle born house"): offer the places that share it.
    said = (_tokens(" ".join(words)) - skip) if top < 0.88 else set()   # "white run" is here, not the camp
    found += [p for p in places() if _compact(p.name) not in skip and p not in found and said & _tokens(p.name)]
    return _dedupe(found)[:limit]


_WEAK = {"white", "black", "river", "stone", "north", "south", "east", "west", "great", "house", "clan", "hall",
         "farm", "camp", "cave", "mine", "manor", "tower", "temple", "shrine", "ruins", "ruin", "watch", "stables",
         "outpost", "lodge", "cabin", "shack", "abandoned", "ancient", "falls", "point", "pass", "hollow", "redoubt",
         "barrow", "quarters", "residence", "trading", "company", "goods", "general", "steel", "college", "frost"}


def _tokens(text: str) -> set[str]:
    """The telling words of a name: single words, and word pairs run together ("battle born" -> battleborn)."""
    w = [x for x in _norm(text).split() if x != "the"]
    out = {x for x in w if len(x) >= 6}
    out |= {a + b for a, b in zip(w, w[1:]) if len(a + b) >= 7}
    return {t for t in out if t not in _WEAK and t not in _COMMON}


def _dedupe(found: list[Place]) -> list[Place]:
    out: list[Place] = []
    seen: set[str] = set()
    for p in found:
        if p.form_id not in seen:
            seen.add(p.form_id)
            out.append(p)
    return out


# ---------------------------------------------------------------- what people say

_NPC_LEAD = (r"(?:\blet\s*'?\s*s\b|\blet\s+us\b|\bi\s*'?\s*ll\b|\bi\s+will\b|\bwe\s*'?\s*ll\b|\bwe\s+will\b"
             r"|\bi\s*'?\s*m\s+(?:going\s+to|gonna)\b|\bi\s+can\b|\bi\s+shall\b|\bwe\s+(?:should|can|shall)\b)")
_NPC_GO = re.compile(
    _NPC_LEAD + r"[^.!?;]{0,30}?\b(?:go|head|walk|travel|set\s+(?:off|out)|get\s+(?:going|moving)|make\s+(?:our|my)\s+way"
    r"|take\s+you|show\s+you|lead\s+you|bring\s+you|come\s+(?:with|along)|join\s+you|accompany\s+you|be\s+off|move)\b"
    r"|\bfollow\s+me\b|\blead\s+the\s+way\b|\bright\s+behind\s+you\b|\bafter\s+you\b|\bon\s+our\s+way\b|\bon\s+my\s+way\b"
    r"|\blet\s*'?\s*s\s+be\s+off\b",
    re.IGNORECASE)
_NPC_NOT_NOW = re.compile(r"\b(?:not|never|no\s+time|rather\s+not|later|tomorrow|tonight|someday|another\s+time|next\s+time"
                          r"|once\s+(?:i|we|my)|when\s+(?:i|we|my)|after\s+(?:i|we|my|the)|first)\b|n\s*'?\s*t\b", re.IGNORECASE)

_PLAYER_GO = re.compile(
    r"\b(?:go|going|head|heading|walk|travel|take\s+me|show\s+me|lead\s+me|bring\s+me|visit|come\s+with\s+me"
    r"|follow\s+me|get\s+to|make\s+(?:our|my)\s+way)\b", re.IGNORECASE)

_YES = re.compile(r"^\W*(?:yes|yeah|yea|yep|yup|aye|sure|ok|okay|alright|all\s+right|right|correct|indeed|of\s+course"
                  r"|let\s*'?\s*s\s+go|lets\s+go|go|do\s+it|that\s+one|sounds\s+good|please)\b", re.IGNORECASE)
_NO = re.compile(r"^\W*(?:no|nope|nah|cancel|never\s*mind|nevermind|not\s+(?:now|yet|right\s+now)|stop|wait|hold\s+on"
                 r"|don\s*'?\s*t)\b", re.IGNORECASE)
_NUMBERS = [
    (1, re.compile(r"\b(?:one|first|1|won|number\s+one)\b", re.IGNORECASE)),
    (2, re.compile(r"\b(?:two|second|2|number\s+two)\b", re.IGNORECASE)),
    (3, re.compile(r"\b(?:three|third|3|number\s+three|tree|free)\b", re.IGNORECASE)),
]
_SHORT_NUMBERS = [(2, re.compile(r"^\W*(?:to|too)\W*$", re.IGNORECASE))]


def npc_agrees_to_go(text: str) -> bool:
    """The NPC's sentence agrees to set off now (a statement, not a question, refusal or 'later')."""
    text = (text or "").replace("’", "'")
    if not text.strip() or text.rstrip().endswith("?"):
        return False
    match = _NPC_GO.search(text)
    if not match:
        return False
    clause = re.split(r"[.!?;]", text[max(0, match.start() - 25):], maxsplit=1)[0]
    clause = re.sub(r"\bafter\s+you\b", "", clause, flags=re.IGNORECASE)
    return not _NPC_NOT_NOW.search(clause)


def player_wants_to_go(text: str) -> bool:
    return bool(_PLAYER_GO.search(text or ""))


# ---------------------------------------------------------------- the planner

@dataclass
class Question:
    npc: str
    choices: list[Place]
    waited: int = 0


@dataclass
class TripPlanner:
    """One per conversation: remembers what the player asked for, the open question and the yes."""

    here: str = ""
    player_name: str = "The player"
    question: Question | None = None
    go: tuple[str, Place] | None = None          # confirmed: fire on the next NPC line
    asked: set[tuple[str, str]] = field(default_factory=set)      # (npc, form_id) asked about already
    declined: set[str] = field(default_factory=set)               # form_ids the player said no to
    started: set[str] = field(default_factory=set)                # NPCs already on their way
    _wanted: list[tuple[int, list[Place]]] = field(default_factory=list)   # (player line no, places asked for)
    _line: int = 0
    notices: list[str] = field(default_factory=list)              # for the game, with the next NPC line

    # -- player side

    def player_said(self, text: str) -> str | None:
        """Read the player's line. Returns a note for the NPC when it answered the question, else None."""
        self._line += 1
        if player_wants_to_go(text):
            found = find_places(text, exclude=[self.here])
            if found:
                self._wanted.append((self._line, found))
        q = self.question
        if q is None:
            return None
        pick = self._answer(text, q)
        if pick == "no":
            self.question = None
            for p in q.choices:
                self.declined.add(p.form_id)
            logger.log(23, f"Living World trips: the player said no to going with {q.npc}")
            return f"{self.player_name} decided not to set off with {q.npc} just now."
        if isinstance(pick, Place):
            self.question = None
            self.go = (q.npc, pick)
            logger.log(23, f"Living World trips: the player said yes: {q.npc} leads them to {pick.name}")
            return (f"{self.player_name} confirmed: {q.npc} now leads {self.player_name} to {pick.name}, "
                    f"setting off as {q.npc} speaks. Keep it short; you are walking there now.")
        if pick == "again":
            self.notices += self._question_lines(q)
            return None
        q.waited += 1
        if q.waited >= PATIENCE:
            logger.log(23, f"Living World trips: no answer about going with {q.npc}; dropping the question")
            self.question = None
        return None

    def _answer(self, text: str, q: Question) -> Any:
        words = _norm(text).split()
        if len(q.choices) > 1:
            if len(words) <= 6:
                for number, pattern in _NUMBERS:
                    if pattern.search(text) and number <= len(q.choices):
                        return q.choices[number - 1]
                for number, pattern in _SHORT_NUMBERS:
                    if pattern.search(text) and number <= len(q.choices):
                        return q.choices[number - 1]
            named = find_places(text, exclude=[self.here])
            for p in named:
                if p in q.choices:
                    return p
            if _NO.search(text):
                return "no"
            if _YES.search(text):
                return "again"     # "yes" to a list: show the numbers once more
            return None
        if _NO.search(text):
            return "no"
        if _YES.search(text):
            return q.choices[0]
        named = find_places(text, exclude=[self.here])
        if named and named[0] == q.choices[0] and len(words) <= 8:
            return q.choices[0]
        return None

    # -- NPC side

    def npc_said(self, npc: str, text: str, can_travel: bool) -> None:
        """An NPC sentence: ask the player when it agrees to set off somewhere."""
        if not can_travel or self.question is not None or self.go is not None or npc in self.started:
            return
        if not npc_agrees_to_go(text):
            return
        choices = find_places(text, exclude=[self.here])
        if not choices:
            recent = [found for line, found in reversed(self._wanted) if self._line - line < RECENT_LINES]
            choices = recent[0] if recent else []
        choices = [p for p in choices if p.form_id not in self.declined and (npc, p.form_id) not in self.asked]
        if not choices:
            return
        q = Question(npc, choices[:MAX_CHOICES])
        for p in q.choices:
            self.asked.add((npc, p.form_id))
        self.question = q
        self.notices += self._question_lines(q)
        logger.log(23, f"Living World trips: {npc} agreed to go; asking the player about "
                       + ", ".join(p.name for p in q.choices))

    def _question_lines(self, q: Question) -> list[str]:
        who = _first_name(q.npc)
        if len(q.choices) == 1:
            return [f"Go with {who} to {q.choices[0].name}? Say yes or no."]
        numbers = ("one", "two", "three")[:len(q.choices)]
        lines = [f"Where to with {who}? Say {', '.join(numbers[:-1])} or {numbers[-1]}, or no:"]
        lines += [f"{i + 1}: {p.name}" for i, p in enumerate(q.choices)]
        return lines

    def take_go(self, offered: set[str]) -> dict | None:
        """The trip the player confirmed, as an action for the NPC's line, or None."""
        if self.go is None:
            return None
        npc, place = self.go
        self.go = None
        self.started.add(npc)
        if LEAD_TO in offered:
            return {"identifier": LEAD_TO, "arguments": {"source": npc, "target": self.player_name, "location": place.name}}
        if TRAVEL_TO in offered:
            return {"identifier": TRAVEL_TO, "arguments": {"source": [npc], "location": place.name, "leave_now": True}}
        return None

    def note_started(self, actions: Iterable[Any]) -> None:
        """A reply that already called LeadTo / TravelTo / Follow: those NPCs are on their way."""
        for action in actions:
            if not isinstance(action, dict) or action.get("identifier") not in _STARTED:
                continue
            source = (action.get("arguments") or {}).get("source")
            for name in source if isinstance(source, list) else [source]:
                if name:
                    self.started.add(name)
                    if self.question is not None and self.question.npc == name:
                        self.question = None
                    if self.go is not None and self.go[0] == name:
                        self.go = None   # the AI started the trip itself on the player's yes

    def take_notices(self) -> list[str]:
        out, self.notices = self.notices, []
        return out


def _first_name(name: str) -> str:
    return (name or "").split(" ")[0] or name


class TripWatcher:
    """One per reply, in the reply parser chain: fires a confirmed trip and asks about new ones."""

    def __init__(self, planner: TripPlanner, actions: Iterable[Any], loaded: Iterable[str] = ()):
        """``actions``: the reply's prompt actions. ``loaded``: identifiers of every action Mantella loaded
        (not switched off in Disabled Actions). LeadTo / TravelTo have no basic-mode prompt, so they are
        only ever in ``loaded``; the game carries them out either way when the line names them."""
        self.planner = planner
        self.offered = ({getattr(a, "identifier", None) for a in actions} | set(loaded)) & _TRIP_ACTIONS

    def note_actions(self, actions: Iterable[Any]) -> None:
        self.planner.note_started(actions)

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.planner.note_started(sentence_actions)
        added: list[str] = []
        action = self.planner.take_go(self.offered)
        if action is not None:
            sentence_actions.append(action)
            added.append(action["identifier"])
            self.planner.note_started([action])
            logger.log(23, f"Living World trips: {action}")
        self.planner.npc_said(speaker_name, text, bool(self.offered))
        return added
