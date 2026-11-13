"""Undress from words: an NPC who says she is taking something off actually does, after the player's yes.

The dialogue model often answers "I suppose it might be time to take a dip without my tunic" in words
only, without starting its line with ``Undress:`` (or calling the tool), so nothing changes in the game.
This module watches for that, the same way trips do (``dialogue/trips.py``):

1. The NPC's own sentence says she is taking something off now ("let me slip out of this dress", "time to
   take a dip without my tunic", "I'll take my boots off", "I'll undress"), the reply has not already
   called ``Undress``, and the NPC is not ruled out (see *What is never overridden*).
2. The game shows a notification: "Fralia is taking off her tunic. Let her? Say yes or no."
3. On "yes", the NPC's next line carries ``Undress`` with that piece of clothing (``item``), so only the
   tunic comes off, unless that line backs out ("Actually, I'd rather not"). On "no" she keeps it on and
   the dialogue model is told the player said so. A question nobody answers lapses after two lines.
4. ``Dress`` (or her saying she will put it back on, through the ordinary ``Dress`` action) puts back what
   ``Undress`` took off.

The yes is the player's, never hers: the prompt only confirms that the player wants it to happen, after
the NPC has already chosen it herself. Nothing here ever asks an NPC to undress or makes one more willing.

What is never overridden (no question is asked, and the model hears that she thought better of it):
- children: never (and the game refuses them too);
- an NPC who is drunk or worse cannot choose to take off her main garment (body, or everything);
- her personality profile (``livingworld/personality``): "no kissing or touching where people can see"
  keeps her clothes on while other adults are close by; "nothing intimate with someone she does not know
  well" keeps them on with a player she hardly knows; "wants no intimacy with anyone" keeps them on in
  front of the player. Boots, gloves, a hat or a cloak are not limited by these.

On with the Living World unless ``MANTELLA_LW_UNDRESS_PROMPT=0``.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from livingworld.actions import UNDRESS

ENV = "MANTELLA_LW_UNDRESS_PROMPT"
_OFF = {"0", "false", "no", "off"}

PATIENCE = 2   # player lines a question waits for an answer
STRANGER_FAMILIARITY = 0.2   # her familiarity with the player below this: "someone she does not know well"

logger = logging.getLogger("livingworld.undress")


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


# ---------------------------------------------------------------- clothing words

# part -> words. The game (MantellaRepository.psc, LwUndressMask) maps each part to armor slots.
PARTS: dict[str, tuple[str, ...]] = {
    "body": ("tunic", "dress", "gown", "robe", "robes", "shirt", "blouse", "jerkin", "smock", "apron", "frock",
             "vest", "top", "cuirass", "armor", "armour", "chainmail", "mail", "outfit", "skirt", "trousers",
             "breeches", "pants", "leggings", "kirtle", "bodice", "corset", "coat", "jacket", "doublet"),
    "feet": ("boots", "boot", "shoes", "shoe", "sandals", "slippers"),
    "hands": ("gloves", "glove", "gauntlets", "gauntlet", "bracers", "bracer", "mittens"),
    "head": ("hat", "hood", "helmet", "helm", "cap", "circlet", "headscarf", "scarf", "bonnet", "headband",
             "headdress", "cowl", "mask"),
    "cloak": ("cloak", "cape", "mantle", "shawl", "fur"),
    "jewelry": ("necklace", "amulet", "pendant", "ring", "rings", "jewelry", "jewellery", "jewels", "earrings",
                "bracelet"),
    "all": ("clothes", "clothing", "everything", "garments", "things", "gear", "attire", "kit", "it all",
            "all of it", "the lot"),
}
MAIN = frozenset({"body", "all"})      # what leaves her bare: the only parts her limits apply to
_WORD_PART = {w: part for part, words in PARTS.items() for w in words}
_ITEM = r"(?:" + "|".join(sorted((re.escape(w) for w in _WORD_PART), key=len, reverse=True)) + r")"

# "I'll", "let me", "time to", "here goes": she is doing it now.
_LEAD = (r"(?:\bi\s*'?\s*ll\b|\bi\s+will\b|\blet\s+me\b|\blet\s*'?\s*s\b|\bi\s*'?\s*m\s+(?:going\s+to|gonna|taking|slipping"
         r"|getting)\b|\bi\s+shall\b|\btime\s+(?:for\s+me\s+)?to\b|\bmight\s+as\s+well\b|\bhere\s+goes\b|\bi\s+think\s+i\s*'?\s*ll\b"
         r"|\bwatch\s+me\b|\bthere\s+we\s+go\b)")
_GAP = r"[^.!?;]{0,50}?"
_POSS = r"(?:my|this|these|the|that|those)\s+(?:\w+\s+)?"   # "my belted tunic", "this old dress"

_TAKE = re.compile(
    _LEAD + _GAP + r"(?:"
    r"\b(?:take|taking|took|get|getting|pull|pulling|slip|slipping|peel|peeling|shrug|shrugging|kick|kicking)\s+"
    + _POSS + r"(?P<a>" + _ITEM + r")\s+off\b"
    r"|\b(?:take|taking|get|getting|pull|pulling|slip|slipping|peel|peeling|shrug|shrugging|kick|kicking|strip|stripping|shed"
    r"|shedding|remove|removing|lose|losing|drop|dropping|loosen|loosening|unlace|unlacing|undo|undoing)\s+(?:off\s+|out\s+of\s+)?"
    + _POSS + r"(?P<b>" + _ITEM + r")\b"
    r"|\bwithout\s+(?:my|this|these|the|that|those)\s+(?:\w+\s+)?(?P<c>" + _ITEM + r")\b"
    r"|\bout\s+of\s+" + _POSS + r"(?P<d>" + _ITEM + r")\b"
    r"|\boff\s+(?:with|comes?|goes|go)\s+" + _POSS + r"(?P<e>" + _ITEM + r")\b"
    r"|\b(?P<all>undress|undressing|strip\s+(?:down|off|bare|naked)|strip|stripping|disrobe|disrobing|get\s+naked"
    r"|get\s+undressed|bare\s+myself|shed\s+(?:everything|it\s+all))\b"
    r")", re.IGNORECASE)

# The clause backs out, waits, or puts it off.
_NOT_NOW = re.compile(r"\b(?:not|never|no|n't|rather\s+not|later|tomorrow|tonight|someday|another\s+time|next\s+time"
                      r"|once|when|after|if|unless|before|maybe|perhaps|might|could|would|should)\b|n\s*'?\s*t\b",
                      re.IGNORECASE)
# "if you don't mind" is politeness, not a condition.
_POLITE = re.compile(r"\bif\s+(?:you\s+(?:don't|do\s+not)\s+mind|you\s+(?:'d\s+|would\s+)?like|you\s+insist|you\s+please"
                     r"|that'?s\s+(?:all\s*right|alright|ok(?:ay)?)|it'?s\s+(?:all\s*right|alright|ok(?:ay)?))\b"
                     r"|\bit\s+might\s+be\s+time\b|\bi\s+might\s+as\s+well\b|\bmight\s+as\s+well\b", re.IGNORECASE)
# Putting it back on: never an undress.
_ON = re.compile(r"\b(?:put|putting|pull|slip|get)\s+(?:\w+\s+){0,3}?(?:back\s+)?on\b|\bget\s+dressed\b|\bdress\s+(?:again|myself)\b",
                 re.IGNORECASE)

# The NPC backs out of it on the line after the player's yes.
_BACK_OUT = re.compile(r"\b(?:changed?\s+my\s+mind|on\s+second\s+thought|second\s+thoughts|actually,?\s+(?:no|i)|rather\s+not"
                       r"|better\s+not|not\s+(?:here|now|yet|after\s+all)|i\s+(?:can'?t|cannot|won'?t|shouldn'?t)"
                       r"|keep\s+(?:my|it|this|them)\b[^.!?]{0,20}\bon\b|no,|no\.|no!)", re.IGNORECASE)

# The player's answer.
_YES = re.compile(r"^\W*(?:yes|yeah|yea|yep|yup|aye|sure|ok|okay|alright|all\s+right|go\s+ahead|go\s+on|do\s+it|please"
                  r"|of\s+course|absolutely|definitely|certainly|by\s+all\s+means|let\s+her|let\s+him|let\s+them)\b",
                  re.IGNORECASE)
_NO = re.compile(r"^\W*(?:no|nope|nah|don'?t|do\s+not|never\s*mind|nevermind|not\s+(?:now|yet|here)|stop|wait|hold\s+on"
                 r"|keep\s+it\s+on|leave\s+it)\b", re.IGNORECASE)


def normalize_item(value: Any) -> str:
    """The ``item`` argument of an ``Undress`` call as one of the game's parts: 'all' unless it names a piece.
    A tool call may say "tunic" or "her boots" instead of "body" / "feet"."""
    words = re.findall(r"[a-z]+", str(value or "").lower())
    for w in words:
        if w in PARTS:
            return w
    for w in words:
        if w in _WORD_PART:
            return _WORD_PART[w]
    for w in words:            # plurals and the like: "shoes" is there, "helmets" is not
        if w.rstrip("s") in _WORD_PART:
            return _WORD_PART[w.rstrip("s")]
    return "all"


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").strip()


@dataclass(frozen=True)
class Offer:
    part: str        # a key of PARTS
    word: str        # the word she used ("tunic"), for the question


def npc_takes_off(text: str) -> Offer | None:
    """What this NPC sentence says she is taking off right now, or None."""
    text = _clean(text)
    if not text or text.endswith("?") or _ON.search(text):
        return None
    for match in _TAKE.finditer(text):
        clause = re.split(r"[.!?;]", text[max(0, match.start() - 30):], maxsplit=1)[0]
        lead_start = match.start() - max(0, match.start() - 30)
        before = clause[:lead_start]
        rest = clause[lead_start:]
        # a "no"/"not" before the lead in the same clause ("I won't, let me ..." is rare; "No, I'll keep ...")
        if _NOT_NOW.search(_POLITE.sub("", rest)) or re.search(r"\b(?:not|never|n't)\b", before, re.IGNORECASE):
            continue
        if match.group("all"):
            return Offer("all", "clothes")
        word = next(match.group(g) for g in ("a", "b", "c", "d", "e") if match.group(g))
        return Offer(_WORD_PART[word.lower()], word.lower())
    return None


def player_answer(text: str) -> str | None:
    """'yes', 'no' or None for the player's line."""
    text = _clean(text)
    if _NO.search(text):
        return "no"
    if _YES.search(text):
        return "yes"
    return None


def backs_out(text: str) -> bool:
    return bool(_BACK_OUT.search(_clean(text)))


# ---------------------------------------------------------------- the planner

# gate(speaker name, part) -> None when she may, else a short reason in plain words ("other people are close by")
Gate = Callable[[str, str], "str | None"]


@dataclass
class Question:
    npc: str
    offer: Offer
    pronoun: str
    waited: int = 0


@dataclass
class UndressPlanner:
    """One per conversation: the open question, the player's yes, and what was already asked."""

    player_name: str = "The player"
    question: Question | None = None
    go: tuple[str, Offer] | None = None              # the player said yes: Undress on her next line
    asked: set[tuple[str, str]] = field(default_factory=set)   # (npc, part) asked about already
    declined: set[tuple[str, str]] = field(default_factory=set)
    notices: list[str] = field(default_factory=list)  # for the game, with the next NPC line
    notes: list[str] = field(default_factory=list)    # for the dialogue model, with the player's next line

    # -- NPC side

    def npc_said(self, npc: str, text: str, can_undress: bool, gate: Gate | None = None,
                 pronoun: str = "their") -> None:
        if not can_undress or self.question is not None or self.go is not None:
            return
        offer = npc_takes_off(text)
        if offer is None:
            return
        key = (npc, offer.part)
        if key in self.asked or key in self.declined:
            return
        self.asked.add(key)
        reason = gate(npc, offer.part) if gate is not None else None
        if reason:
            logger.log(23, f"Living World undress: {npc} said she would take off her {offer.word}, but not asking: {reason}")
            self.notes.append(f"{npc} thought better of taking off {pronoun} {offer.word} and keeps it on ({reason}).")
            return
        self.question = Question(npc, offer, pronoun)
        self.notices.append(self._question_line(self.question))
        logger.log(23, f"Living World undress: {npc} said she is taking off {pronoun} {offer.word}; asking the player")

    def _question_line(self, q: Question) -> str:
        who = q.npc.split(" ")[0] or q.npc
        what = f"{q.pronoun} {q.offer.word}" if q.offer.part != "all" else f"{q.pronoun} clothes"
        return f"{who} is taking off {what}. Let {who}? Say yes or no."

    # -- player side

    def player_said(self, text: str) -> str | None:
        """The player's line. Returns a note for the dialogue model when it answered the question."""
        q = self.question
        if q is None:
            return None
        answer = player_answer(text)
        what = q.offer.word if q.offer.part != "all" else "clothes"
        if answer == "yes":
            self.question = None
            self.go = (q.npc, q.offer)
            logger.log(23, f"Living World undress: the player said yes: {q.npc} takes off {q.pronoun} {what}")
            return (f"{self.player_name} is happy for {q.npc} to take off {q.pronoun} {what}. {q.npc} does it now as "
                    f"{q.npc} speaks, if {q.npc} still wants to; {q.npc} may still change {q.pronoun} mind and keep it on.")
        if answer == "no":
            self.question = None
            self.declined.add((q.npc, q.offer.part))
            logger.log(23, f"Living World undress: the player said no: {q.npc} keeps {q.pronoun} {what} on")
            return f"{self.player_name} would rather {q.npc} kept {q.pronoun} {what} on, so {q.npc} leaves it on."
        q.waited += 1
        if q.waited >= PATIENCE:
            logger.log(23, f"Living World undress: no answer about {q.npc}'s {what}; dropping the question")
            self.question = None
        return None

    def take_go(self, npc: str, text: str) -> dict | None:
        """``Undress`` for the NPC's line after the player's yes, unless this line backs out."""
        if self.go is None or self.go[0] != npc:
            return None
        _, offer = self.go
        self.go = None
        if backs_out(text):
            logger.log(23, f"Living World undress: {npc} changed her mind about the {offer.word}; nothing comes off")
            return None
        return {"identifier": UNDRESS, "arguments": {"source": npc, "item": offer.part}}

    def note_undressed(self, actions: Iterable[Any]) -> None:
        """The reply already called ``Undress`` for someone: no question about them on top."""
        for action in actions:
            if not isinstance(action, dict) or action.get("identifier") != UNDRESS:
                continue
            source = (action.get("arguments") or {}).get("source")
            names = source if isinstance(source, list) else [source]
            for name in names:
                if self.question is not None and self.question.npc == name:
                    self.question = None
                if self.go is not None and self.go[0] == name:
                    self.go = None

    def take_notices(self) -> list[str]:
        out, self.notices = self.notices, []
        return out

    def take_notes(self) -> list[str]:
        out, self.notes = self.notes, []
        return out


class UndressWatcher:
    """One per reply, in the reply parser chain (``dialogue/promises.py`` runs it with the other keepers)."""

    def __init__(self, planner: UndressPlanner, offered: Iterable[str], gate: Gate | None = None,
                 pronoun_of: Callable[[str], str] | None = None):
        self.planner = planner
        self.can_undress = UNDRESS in set(offered)
        self.gate = gate
        self.pronoun_of = pronoun_of or (lambda _name: "their")
        self._started: set[str] = set()

    def note_actions(self, actions: Iterable[Any]) -> None:
        actions = list(actions)
        self.planner.note_undressed(actions)
        for action in actions:
            if isinstance(action, dict) and action.get("identifier") == UNDRESS:
                source = (action.get("arguments") or {}).get("source")
                self._started.update(source if isinstance(source, list) else [source])

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.note_actions(sentence_actions)
        if not self.can_undress or speaker_name in self._started:
            return []
        action = self.planner.take_go(speaker_name, text)
        if action is not None:
            sentence_actions.append(action)
            self._started.add(speaker_name)
            logger.log(23, f"Living World undress: {action}")
            return [UNDRESS]
        self.planner.npc_said(speaker_name, text, True, self.gate, self.pronoun_of(speaker_name))
        return []
