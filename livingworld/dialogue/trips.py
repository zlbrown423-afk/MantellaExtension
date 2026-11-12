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

When the line only says "the farm", "home" or "the mill", the place is taken from what was said earlier
in the conversation ("I run the Battle-Born Farm") or from the NPC's own bio, matched on that word. When
the line names no place at all ("Follow me!"), the place named most recently in the last few lines counts.
When the player asked the NPC to follow ("come on, follow me") and the NPC agrees with no place in mind
("I'm right behind you"), the question is "Have Alfhild follow you?" and a yes carries ``Follow``.

Stopping: "stop leading", "cancel the trip", "let's turn back" (or a plain "stop" / "wait here" while
an NPC from this conversation is leading) puts ``CancelTravel`` on the NPC's next line, which in the game
now ends a ``LeadTo`` as well as a ``TravelTo``. The game also ends a lead on its own on arrival.

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
CANCEL_TRAVEL = "mantella_npc_canceltravel"
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


def places_in(text: str, exclude: Iterable[str] = ()) -> list[Place]:
    """Every place named exactly in ``text`` (a bio, or a line that mentions places in passing)."""
    words = [w for w in _norm(text).split()]
    skip = {_compact(e) for e in exclude if e}
    grams = set()
    for n in range(1, 6):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] == "the":
                gram = gram[1:]
            if gram and " ".join(gram) not in _COMMON:
                grams.add("".join(gram))
    found = [p for p in places() if _compact(p.name) in grams and _compact(p.name) not in skip
             and len(_compact(p.name)) >= 5]
    # "Battle-Born Farm" says it all; drop "Battle-Born" style names found inside a longer one
    found = [p for p in found if not any(q is not p and _compact(p.name) in _compact(q.name) for q in found)]
    return _dedupe(found)


# Words that stand for a place the people talking already know about: "head to the farm", "back home".
_GENERIC = {
    "farm": ("farm",), "mill": ("mill",), "inn": ("inn", "mare", "barb", "flagon"), "mine": ("mine",),
    "camp": ("camp",), "house": ("house", "home", "manor"), "home": ("home", "house", "manor", "farm"),
    "shop": ("goods", "trading", "store", "shop", "smith", "forge"), "store": ("goods", "trading", "store", "shop"),
    "forge": ("forge", "smith"), "temple": ("temple",), "hall": ("hall",), "keep": ("keep",), "lodge": ("lodge",),
    "stables": ("stables",), "tower": ("tower",), "shack": ("shack",), "cabin": ("cabin",), "palace": ("palace",),
    "orchard": ("orchard",), "fields": ("farm",), "field": ("farm",),
}
_GENERIC_RE = re.compile(r"\b(?:the|my|our|your|her|his|their)\s+(" + "|".join(sorted(_GENERIC, key=len, reverse=True))
                         + r")\b|\b(home)\b", re.IGNORECASE)


def generic_words(text: str) -> list[str]:
    """The stand-in place words in ``text`` ("the farm", "home"), in order."""
    return [(m.group(1) or m.group(2)).lower() for m in _GENERIC_RE.finditer(text or "")]


def fits_generic(word: str, place: Place) -> bool:
    name = set(_norm(place.name).split())
    return any(w in name for w in _GENERIC.get(word, (word,)))


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

# The NPC agrees to come along behind the player (not to lead): "I'm right behind you", "lead the way".
_NPC_FOLLOWS = re.compile(
    r"\bright\s+behind\s+you\b|\bafter\s+you\b|\b(?:i\s*'?\s*ll|i\s+will|i\s*'?\s*m\s+going\s+to|i\s+can)\s+"
    r"(?:follow\s+you|come\s+(?:with|along)|join\s+you|accompany\s+you|tag\s+along)\b"
    r"|(?<!\bi\s)(?<!\bi'll\s)(?<!\blet\sme\s)\blead\s+the\s+way\b|\byou\s+lead\b|\bwith\s+you\b",
    re.IGNORECASE)
_NPC_LEADS = re.compile(r"\b(?:i\s*'?\s*ll|i\s+will|let\s+me|i\s*'?\s*m\s+going\s+to)\s+(?:lead|show|take)\b|\bfollow\s+me\b",
                        re.IGNORECASE)
_PLAYER_FOLLOW = re.compile(r"\b(?:follow\s+me|come\s+with\s+me|come\s+along|come\s+on|join\s+me|tag\s+along"
                            r"|keep\s+up|stay\s+close|stick\s+with\s+me|let\s*'?\s*s\s+go)\b", re.IGNORECASE)

_PLAYER_GO = re.compile(
    r"\b(?:go|going|head|heading|walk|travel|take\s+me|show\s+me|lead\s+me|bring\s+me|visit|come\s+with\s+me"
    r"|follow\s+me|get\s+to|make\s+(?:our|my)\s+way)\b", re.IGNORECASE)

_YES = re.compile(r"^\W*(?:yes|yeah|yea|yep|yup|aye|sure|ok|okay|alright|all\s+right|right|correct|indeed|of\s+course"
                  r"|let\s*'?\s*s\s+go|lets\s+go|go|do\s+it|that\s+one|sounds\s+good|please)\b", re.IGNORECASE)
# Saying yes by going along with it: "Lead the way, I'm following you."
_GO_ALONG = re.compile(r"\blead\s+the\s+way\b|\bi\s*'?\s*m\s+(?:following|right\s+behind|coming|with\s+you)\b"
                       r"|\bafter\s+you\b|\blet\s*'?\s*s\s+(?:go|do\s+(?:it|this)|head\s+out|get\s+going|move)\b"
                       r"|\bshow\s+me\s+the\s+way\b", re.IGNORECASE)
_NO = re.compile(r"^\W*(?:no|nope|nah|cancel|never\s*mind|nevermind|not\s+(?:now|yet|right\s+now)|stop|wait|hold\s+on"
                 r"|don\s*'?\s*t)\b", re.IGNORECASE)
# Ending a trip that is under way. Explicit enough to count in any conversation:
_STOP_TRIP = re.compile(
    r"\b(?:stop|quit)\s+(?:leading(?:\s+me)?|walking|taking\s+me|the\s+trip|this\s+trip)\b"
    r"|\b(?:cancel|forget|call\s+off|abandon)\s+(?:the\s+|this\s+|our\s+)?(?:trip|journey|travel|walk)\b"
    r"|\bnever\s*mind\s+(?:the\s+|this\s+)?(?:trip|journey|going)\b"
    r"|\b(?:let\s*'?\s*s|we\s+should)\s+(?:not\s+go|turn\s+(?:back|around)|stop\s+here)\b"
    r"|\bchange\s+of\s+plans?\b|\bwe\s*'?\s*re\s+not\s+going\b",
    re.IGNORECASE)
# ...and short ones that only count while an NPC from this conversation is leading: "Stop!", "Wait here,
# Alfhild." Every word must be one of these, apart from up to two (a name).
_STOP_SHORT = re.compile(r"^\W*(?:stop|halt|wait|hold\s+on|hold\s+up|stay\s+here|that\s*'?\s*s\s+far\s+enough)\b",
                         re.IGNORECASE)
_STOP_WORDS = {"stop", "halt", "wait", "hold", "on", "up", "stay", "here", "there", "now", "please", "right",
               "thats", "that", "s", "far", "enough", "a", "moment", "second", "minute", "lets", "let", "us"}


def player_stops_trip(text: str, leading: bool = False) -> bool:
    """The player calls off a trip under way. Short "stop" / "wait" only counts while someone is leading."""
    text = (text or "").replace("’", "'")
    if _STOP_TRIP.search(text):
        return True
    if not leading or not _STOP_SHORT.search(text):
        return False
    words = _norm(text.replace("'", "")).split()
    return len(words) <= 6 and sum(w not in _STOP_WORDS for w in words) <= 2


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


def npc_follows(text: str) -> bool:
    """The agreeing line has the NPC coming along behind the player rather than leading."""
    text = (text or "").replace("’", "'")
    return bool(_NPC_FOLLOWS.search(text)) and not _NPC_LEADS.search(text)


def player_asks_follow(text: str) -> bool:
    return bool(_PLAYER_FOLLOW.search((text or "").replace("’", "'")))


# ---------------------------------------------------------------- the planner

@dataclass
class Question:
    npc: str
    choices: list[Place]          # empty: "Have them follow you?"
    waited: int = 0

    @property
    def follow(self) -> bool:
        return not self.choices


@dataclass
class TripPlanner:
    """One per conversation: remembers what the player asked for, the open question and the yes."""

    here: str = ""
    player_name: str = "The player"
    question: Question | None = None
    go: tuple[str, Place | None] | None = None   # confirmed: fire on the next NPC line (None: follow the player)
    asked: set[tuple[str, str]] = field(default_factory=set)      # (npc, form_id) asked about already
    declined: set[str] = field(default_factory=set)               # form_ids the player said no to
    started: set[str] = field(default_factory=set)                # NPCs already on their way
    leading: set[str] = field(default_factory=set)                # of those, leading the player somewhere
    cancel: bool = False                                          # the player said stop: CancelTravel next line
    _wanted: list[tuple[int, list[Place]]] = field(default_factory=list)   # (player line no, places asked for)
    _mentioned: list[tuple[int, list[Place]]] = field(default_factory=list)   # (line no, places anyone named)
    _follow_asked: int | None = None                                      # player line that said "follow me"
    known: dict[str, list[Place]] = field(default_factory=dict)          # NPC -> places in their bio
    _line: int = 0
    notices: list[str] = field(default_factory=list)              # for the game, with the next NPC line

    # -- player side

    def player_said(self, text: str) -> str | None:
        """Read the player's line. Returns a note for the NPC when it answered the question, else None."""
        self._line += 1
        if self.question is None and player_stops_trip(text, leading=bool(self.leading)):
            who = ", ".join(sorted(self.leading)) or "anyone leading the way"
            self.cancel = True
            self.go = None
            self.started -= self.leading
            self.leading = set()
            logger.log(23, f"Living World trips: the player called off the trip ({who})")
            return (f"{self.player_name} called off the trip: {who} stops leading and stays here. "
                    f"Acknowledge it briefly.")
        if player_wants_to_go(text):
            found = find_places(text, exclude=[self.here]) or self._generic(text, None)
            if found:
                self._wanted.append((self._line, found))
        self._mention(text)
        if player_asks_follow(text):
            self._follow_asked = self._line
        q = self.question
        if q is None:
            return None
        pick = self._answer(text, q)
        if pick == "no":
            self.question = None
            for p in q.choices:
                self.declined.add(p.form_id)
            if q.follow:
                self.declined.add("follow:" + q.npc)
                logger.log(23, f"Living World trips: the player said no to {q.npc} following")
                return f"{self.player_name} decided {q.npc} should not follow just now."
            logger.log(23, f"Living World trips: the player said no to going with {q.npc}")
            return f"{self.player_name} decided not to set off with {q.npc} just now."
        if pick == "follow":
            self.question = None
            self.go = (q.npc, None)
            logger.log(23, f"Living World trips: the player said yes: {q.npc} follows them")
            return (f"{self.player_name} confirmed: {q.npc} now follows {self.player_name}, setting off as "
                    f"{q.npc} speaks. Keep it short; you are coming along now.")
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
        if q.follow:
            if _NO.search(text):
                return "no"
            return "follow" if _YES.search(text) or player_asks_follow(text) or _GO_ALONG.search(text) else None
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
        if _YES.search(text) or _GO_ALONG.search(text.replace("’", "'")):
            return q.choices[0]
        named = find_places(text, exclude=[self.here])
        if named and named[0] == q.choices[0] and len(words) <= 8:
            return q.choices[0]
        return None

    # -- NPC side

    def npc_said(self, npc: str, text: str, can_travel: bool, can_follow: bool = False) -> None:
        """An NPC sentence: ask the player when it agrees to set off somewhere."""
        if (not can_travel and not can_follow) or self.question is not None or self.go is not None \
                or npc in self.started:
            self._mention(text)
            return
        agrees = npc_agrees_to_go(text)
        if not agrees:
            self._mention(text)
            return
        choices = find_places(text, exclude=[self.here]) or self._generic(text, npc)
        if not choices and not generic_words(text):
            recent = [found for line, found in reversed(self._wanted) if self._line - line < RECENT_LINES]
            recent += [found for line, found in reversed(self._mentioned) if self._line - line < RECENT_LINES]
            choices = recent[0] if recent else []
        self._mention(text)
        if (not choices and npc_follows(text) and can_follow and self._follow_asked is not None
                and self._line - self._follow_asked < RECENT_LINES and "follow:" + npc not in self.declined
                and (npc, "follow") not in self.asked):
            self.asked.add((npc, "follow"))
            self.question = Question(npc, [])
            self.notices += self._question_lines(self.question)
            logger.log(23, f"Living World trips: {npc} agreed to come along; asking the player whether they follow")
            return
        choices = [p for p in choices if p.form_id not in self.declined and (npc, p.form_id) not in self.asked]
        if not choices or not can_travel:
            return
        q = Question(npc, choices[:MAX_CHOICES])
        for p in q.choices:
            self.asked.add((npc, p.form_id))
        self.question = q
        self.notices += self._question_lines(q)
        logger.log(23, f"Living World trips: {npc} agreed to go; asking the player about "
                       + ", ".join(p.name for p in q.choices))

    def _mention(self, text: str) -> None:
        found = places_in(text, exclude=[self.here])
        if found:
            self._mentioned.append((self._line, found))

    def _generic(self, text: str, npc: str | None) -> list[Place]:
        """'the farm', 'home': the matching place named lately in this conversation, else from the bio."""
        for word in generic_words(text):
            for line, found in reversed(self._mentioned + self._wanted):
                fit = [p for p in found if fits_generic(word, p) and _compact(p.name) != _compact(self.here)]
                if fit:
                    return fit[:1]
            pool = self.known.get(npc, []) if npc else [p for ps in self.known.values() for p in ps]
            fit = _dedupe([p for p in pool if fits_generic(word, p) and _compact(p.name) != _compact(self.here)])
            if fit:
                return fit[:MAX_CHOICES]
        return []

    def learn_bio(self, npc: str, bio: str) -> None:
        """Places named in an NPC's bio, for 'the farm' / 'home' in their lines."""
        if npc and npc not in self.known:
            self.known[npc] = places_in(bio or "", exclude=[self.here])

    def _question_lines(self, q: Question) -> list[str]:
        who = _first_name(q.npc)
        if q.follow:
            return [f"Have {who} follow you? Say yes or no."]
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
        if place is None:
            return {"identifier": FOLLOW, "arguments": {"source": [npc]}} if FOLLOW in offered else None
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
                    if action.get("identifier") in _TRIP_ACTIONS:
                        self.leading.add(name)
                    if self.question is not None and self.question.npc == name:
                        self.question = None
                    if self.go is not None and self.go[0] == name:
                        self.go = None   # the AI started the trip itself on the player's yes

    def take_cancel(self, offered: set[str]) -> dict | None:
        """CancelTravel for the NPC's line after the player said stop, or None."""
        if not self.cancel:
            return None
        self.cancel = False
        if CANCEL_TRAVEL not in offered:
            logger.log(23, "Living World trips: CancelTravel is switched off; the trip can't be called off from here")
            return None
        return {"identifier": CANCEL_TRAVEL, "arguments": {}}

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
        self.offered = ({getattr(a, "identifier", None) for a in actions} | set(loaded)) & (_TRIP_ACTIONS | {FOLLOW, CANCEL_TRAVEL})

    def note_actions(self, actions: Iterable[Any]) -> None:
        self.planner.note_started(actions)

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.planner.note_started(sentence_actions)
        added: list[str] = []
        stop = self.planner.take_cancel(self.offered)
        if stop is not None:
            sentence_actions.append(stop)
            added.append(stop["identifier"])
            logger.log(23, f"Living World trips: {stop}")
        action = self.planner.take_go(self.offered)
        if action is not None:
            sentence_actions.append(action)
            added.append(action["identifier"])
            self.planner.note_started([action])
            logger.log(23, f"Living World trips: {action}")
        self.planner.npc_said(speaker_name, text, bool(self.offered & _TRIP_ACTIONS), FOLLOW in self.offered)
        return added
