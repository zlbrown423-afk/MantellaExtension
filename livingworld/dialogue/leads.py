"""Leads: ask about someone again without saying their name.

Speech-to-text mangles Skyrim names ("vault" for Vald, "pond pron" for the Pawned Prawn), so asking
several people about the same person means fighting the microphone each time. A lead is a name the
player has been asking around about. Once one has come up, a short phrase that speech-to-text handles
easily stands in for it:

    "Have you seen the man I'm looking for?"   -> the latest person
    "Do you know the place I'm looking for?"   -> the latest place
    "Any word on my lead?"                     -> the latest lead of any kind

The player's words are never rewritten: the line carries a note saying who is meant.

How a lead is learned, all from conversation, since the game cannot tell which quest is tracked:

- ``heard``: the player asks about a name the misheard-word list knows ("a man named vault" -> Vald).
- ``npc_named``: the player asks about someone and the NPC's answer holds a name that sounds like a word
  the player said. NPC lines are spelled right, so "Vald? Haven't seen him." fixes "Where is vault?".
- ``said``: the player says a name right and the NPC repeats it.

Names the player asks about again are refreshed, so the newest lead wins. Rows carry the timeline, so
loading an older save forgets leads learned after it. On with the Living World unless
``MANTELLA_LW_LEADS=0``.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from typing import Iterable

from livingworld.dialogue import hearing

LEADS_ENV = "MANTELLA_LW_LEADS"
_OFF = {"0", "false", "no", "off"}
KEEP = 5   # leads remembered at once; older ones drop off the list

KINDS = ("person", "place", "thing")


def leads_enabled() -> bool:
    return os.environ.get(LEADS_ENV, "").strip().lower() not in _OFF


@dataclass(frozen=True)
class Lead:
    name: str
    kind: str
    game_time: float


@dataclass(frozen=True)
class Learned:
    name: str
    kind: str
    source: str
    heard_as: str | None


# ---- standing in for the name ----
_ME = r"(?:i'?m|im|i\s+am|i've\s+been|i\s+have\s+been|i\s+was|we'?re|we\s+are)"
_AFTER = r"(?:looking\s+for|after|searching\s+for|hunting(?:\s+for)?|asking\s+(?:about|after)|trying\s+to\s+find|tracking(?:\s+down)?)"
_PERSON = r"(?:man|woman|person|guy|fellow|fella|gentleman|lady|one|someone|somebody|lad|lass|elf|bloke)"
_PLACE = r"(?:place|spot|location|tavern|inn|building|house|shop|cave|keep)"
_THING = r"(?:thing|item|object)"
_SHORTCUTS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(rf"\b(?:the|that|this)\s+{_PERSON}\s+(?:that\s+|who\s+)?{_ME}\s+{_AFTER}\b", re.I), "person"),
    (re.compile(rf"\b(?:the|that|this)\s+{_PLACE}\s+(?:that\s+)?{_ME}\s+{_AFTER}\b", re.I), "place"),
    (re.compile(rf"\b(?:the|that|this)\s+{_THING}\s+(?:that\s+)?{_ME}\s+{_AFTER}\b", re.I), "thing"),
    (re.compile(rf"\bwho\s+{_ME}\s+{_AFTER}\b", re.I), "person"),
    (re.compile(rf"\bwhat\s+{_ME}\s+{_AFTER}\b", re.I), None),
    (re.compile(r"\bmy\s+(?:lead|target|mark|quarry|quest\s+target)\b", re.I), None),
]


def shortcut(text: str) -> tuple[str, str | None] | None:
    """(the phrase as heard, the kind of lead it asks for, or None for any) when the line stands in
    for a lead's name. None when it does not."""
    for pattern, kind in _SHORTCUTS:
        m = pattern.search(text or "")
        if m:
            return m.group(0), kind
    return None


def pick(leads: list[Lead], kind: str | None) -> Lead | None:
    """The newest lead of that kind (any kind when None, or when there is none of that kind)."""
    if not leads:
        return None
    ordered = sorted(leads, key=lambda l: l.game_time, reverse=True)
    if kind:
        for lead in ordered:
            if lead.kind == kind:
                return lead
    return ordered[0]


def shortcut_note(text: str, leads: list[Lead], player_name: str) -> str | None:
    """A narration note saying who or what the player means, or None."""
    found = shortcut(text)
    if not found:
        return None
    phrase, kind = found
    lead = pick(leads, kind)
    if lead is None:
        return None
    return (f'When {player_name} says "{phrase}", they mean {lead.name}, the {lead.kind} {player_name} '
            f"has been asking around about.")


# ---- learning ----
# Words the misheard list fixes that are not something to go looking for
_NOT_LEADS = {
    "thane", "jarl", "housecarl", "stormcloak", "dragonborn", "dovahkiin", "septims", "thieves guild",
    "greybeards", "thu'um", "skyrim",
}
_PLACE_NAMES = {
    "whiterun", "riverwood", "windhelm", "dragonsreach", "helgen", "bleak falls barrow", "falkreath",
    "winterhold", "pawned prawn", "ragged flagon", "riften", "mistveil keep", "honeyside", "bee and barb",
    "jorrvaskr", "solitude", "markarth", "morthal", "dawnstar", "ratway",
}
_PLACE_WORDS = {
    "keep", "inn", "barrow", "cave", "mine", "hall", "fort", "tower", "ratway", "cistern", "flagon", "prawn",
    "house", "temple", "camp", "farm", "mill", "lodge", "hold", "city", "village", "grotto", "ruin", "ruins",
    "shrine", "sanctuary", "warrens", "market", "docks", "bridge", "pass", "hollow", "den",
}
_THING_WORDS = {
    "amulet", "ring", "sword", "crown", "book", "journal", "horn", "key", "staff", "blade", "shield",
    "dagger", "bow", "axe", "mace", "letter", "note", "gem", "stone", "claw", "circlet", "scroll", "map",
}
# Capitalised words in an NPC's line that are not names
_NOT_NAMES = hearing._COMMON | {
    "i", "i'm", "i've", "i'd", "i'll", "a", "an", "the", "you", "he", "she", "it", "we", "they", "my", "his",
    "her", "our", "no", "yes", "aye", "nay", "oh", "ah", "well", "now", "so", "but", "and", "or", "if", "not",
    "who", "why", "how", "hmm", "ha", "gods", "divines", "talos", "mara", "shor", "ysmir", "sir", "madam",
    "friend", "stranger", "traveler", "traveller", "go", "come", "look", "listen", "don't", "can't",
    "haven't", "hasn't", "isn't", "wasn't", "didn't", "won't", "he's", "she's", "it's", "that's", "there's",
    "what's", "where's", "who's", "let", "let's", "try", "ask", "perhaps", "aye", "nope", "sure", "just",
    "never", "only", "some", "any", "one", "last", "first", "tell", "keep", "be", "do", "did", "does",
    "have", "has", "had", "is", "was", "are", "were", "will", "would", "can", "could", "may", "might",
    "should", "must", "on", "in", "at", "to", "for", "from", "of", "by", "with", "as", "up", "down", "out",
    "off", "into", "near", "far", "after", "still", "yet", "all", "every", "each", "most", "many", "much",
    "jarl", "thane", "housecarl", "guard", "guards", "septims", "skyrim", "dragonborn",
}
_NAME_RUN = re.compile(r"[A-Z][A-Za-z'\-]+(?:\s+(?:and|of|the)\s+[A-Z][A-Za-z'\-]+|\s+[A-Z][A-Za-z'\-]+)*")


def kind_of(name: str, context: str = "") -> str:
    """Person, place or thing, from the name itself and the words around it."""
    low = name.lower()
    words = set(re.findall(r"[a-z']+", low))
    if low in _PLACE_NAMES or words & _PLACE_WORDS:
        return "place"
    if words & _THING_WORDS:
        return "thing"
    if context:
        before = re.search(rf"\b(in|at|to|into|inside|near|from|towards?)\s+(?:the\s+)?{re.escape(name)}\b",
                           context, re.I)
        if before:
            return "place"
    return "person"


def names_in(npc_text: str, skip: Iterable[str] = ()) -> list[str]:
    """Proper names in an NPC's line ("Vald", "the Pawned Prawn" -> "Pawned Prawn"), skipping ``skip``
    (the people in the scene) and capitalised everyday words."""
    skipped = {s.lower() for name in skip for s in [name, *(name or "").split()] if s}
    out: list[str] = []
    for m in _NAME_RUN.finditer(npc_text or ""):
        words = m.group(0).split()
        # trim everyday words off both ends ("Yes Vald" -> "Vald")
        while words and words[0].lower().strip("'") in _NOT_NAMES:
            words.pop(0)
        while words and words[-1].lower().strip("'") in _NOT_NAMES | {"and", "of", "the"}:
            words.pop()
        if not words:
            continue
        name = " ".join(words)
        name = re.sub(r"'s$", "", name)
        if len(name) < 3 or name.lower() in skipped or name.lower() in _NOT_LEADS:
            continue
        if name not in out:
            out.append(name)
    return out


def _said_like(player_text: str, name: str) -> tuple[str, bool] | None:
    """(the words the player said for ``name``, whether they said it right), or None."""
    words = list(re.finditer(r"[A-Za-z']+", player_text))
    target = hearing._norm(name)
    span = len(name.split())
    best = None
    for size in sorted({max(1, span - 1), span, span + 1}):
        for i in range(len(words) - size + 1):
            heard = player_text[words[i].start():words[i + size - 1].end()]
            if hearing._norm(heard) == target:
                return heard, True
            if size == 1 and heard.lower().strip("'") in hearing._COMMON_SHORT:
                continue
            if hearing.sounds_alike(heard, name, 0.6):
                score = difflib.SequenceMatcher(None, hearing._norm(heard), target).ratio()
                if best is None or score > best[0]:
                    best = (score, heard)
    return (best[1], False) if best else None


def from_player(text: str, names: Iterable[str] = ()) -> list[Learned]:
    """Leads in a player's question that the misheard-word list can name ("named vault" -> Vald)."""
    if not text or not hearing.is_question(text) or shortcut(text):
        return []
    out = []
    for heard, _, bare in hearing.matches(text, names):
        if bare.lower() in _NOT_LEADS or bare.startswith("thane of"):
            continue
        out.append(Learned(bare, kind_of(bare, text), "heard", heard))
    return out


def from_reply(player_text: str, npc_text: str, skip: Iterable[str] = ()) -> list[Learned]:
    """Leads in an NPC's answer to the player's question: names in the answer that sound like words the
    player said."""
    if not player_text or not hearing.is_question(player_text) or shortcut(player_text):
        return []
    out = []
    for name in names_in(npc_text, skip):
        said = _said_like(player_text, name)
        if said is None:
            continue
        heard, right = said
        out.append(Learned(name, kind_of(name, npc_text + " " + player_text),
                           "said" if right else "npc_named", None if right else heard))
    return out


# ---- the store ----
def current(db, limit: int = KEEP) -> list[Lead]:
    """The player's newest leads in this timeline, one per name (SQLite takes the name and kind from the
    row holding the MAX)."""
    rows = db.execute(
        "SELECT l.name, l.kind, MAX(l.game_time) FROM player_lead l JOIN v_timeline_scope s"
        " ON s.timeline_id = l.timeline_id AND l.game_time <= s.cutoff_gt"
        " GROUP BY lower(l.name) ORDER BY 3 DESC LIMIT ?", (limit,)).fetchall()
    return [Lead(name, kind, gt) for name, kind, gt in rows]


def remember(db, learned: Learned, game_time: float, conv_id: int | None) -> None:
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    db.execute("INSERT INTO player_lead (timeline_id, name, kind, source, heard_as, conv_id, game_time)"
               " VALUES (?,?,?,?,?,?,?)",
               (timeline, learned.name, learned.kind, learned.source, learned.heard_as, conv_id, game_time))
