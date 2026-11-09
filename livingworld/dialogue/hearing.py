"""Misheard speech: let NPCs catch lines that speech-to-text got wrong.

The player's voice goes through speech-to-text before an NPC hears it, and small STT models turn
Skyrim words into English ones ("As thane of Whiterun" arrives as "As stain of white run"). Two parts:

- ``prompt_lines``: a standing rule in each NPC's prompt. When a line makes no sense, the NPC
  checks the word they think was meant ("Gerdur, you say?") or asks the player to say it again,
  instead of answering it literally.
- ``notes``: for each player line, a short note naming likely mishearings, from a list of
  commonly misheard Skyrim words and from near-misses of the names of the people in the scene.
  The note is a hint for the NPC; the player's words are never rewritten.

On with the Living World unless ``MANTELLA_LW_HEARING=0``.
"""
from __future__ import annotations

import difflib
import os
import re
from typing import Iterable

HEARING_ENV = "MANTELLA_LW_HEARING"
_OFF = {"0", "false", "no", "off"}

_HOLDS = r"(?:whiterun|white\s*run|white\s*rum|riften|rift\s*in|solitude|windhelm|wind\s*helm|markarth|mark\s*arth" \
         r"|falkreath|fall\s*creeth|falcrest|morthal|dawnstar|dawn\s*star|winterhold|winter\s*hold|the\s+reach|the\s+pale|the\s+rift" \
         r"|hjaalmarch|eastmarch|haafingar)"
_THANE_SOUNDS = r"(?:stain|stane|stained|thain|thayne|thing|fain|feign|sane|sain|vein|vane|thin)"

# (what the STT wrote, what was likely meant). Patterns are case-insensitive and whole-word.
# Keep each one specific enough that it is rarely the player's real words.
MISHEARD: list[tuple[str, str]] = [
    # titles
    (rf"\b{_THANE_SOUNDS}\s+of\s+{_HOLDS}\b", "thane of ..."),
    (rf"\b(?:my|a|your|the|their)\s+{_THANE_SOUNDS}\b(?=\s*[,.!?]|\s*$)", "thane"),
    (r"\bhouse\s*(?:carl|curl|girl|call)s?\b", "housecarl"),
    (r"\byarl\b|\bjar\s+(?:of\s+)?(?:whiterun|white\s*run|balgruuf|ulfric|elisif|siddgeir|idgrod|skald|laila|igmund|korir)\b", "Jarl"),
    # places
    (r"\bwhite\s+(?:run|rum|ran|rune)\b", "Whiterun"),
    (r"\briver\s+(?:wood|would|word)\b", "Riverwood"),
    (r"\bwind\s+(?:helm|helm's|elm)\b", "Windhelm"),
    (r"\bdragon'?s?\s+(?:reach|ridge|reech)\b", "Dragonsreach"),
    (r"\bhell\s*gen\b|\bhelgan\b|\bhelga's\b", "Helgen"),
    (r"\bbleak\s+falls?\s+(?:barrel|burrow|borrow|bureau)\b", "Bleak Falls Barrow"),
    (r"\bfall\s*creeth\b|\bfalcrest\b|\bfalkreth\b", "Falkreath"),
    (r"\bwinter\s+hold\b", "Winterhold"),
    (r"\bsky\s+rim\b|\bsky\s+room\b", "Skyrim"),
    # Riften
    (r"\b(?:pond|pawn|pawned|ponned|pond's)\s+(?:pron|prawn|prong|prawns|prone|ron)\b", "Pawned Prawn"),
    (r"\bragged\s+(?:flag\s*on|flaggin|flagging|wagon|dragon)\b", "Ragged Flagon"),
    (r"\b(?:rifton|rift\s+ton|riff\s+ten|rift\s+ten)\b", "Riften"),
    (r"\bmist\s*(?:vale|veil|whale)\s+(?:keep|cape)\b", "Mistveil Keep"),
    (r"\b(?:honey\s+side|honey\s+cide)\b", "Honeyside"),
    (r"\bbee\s+(?:and|in|n)\s+(?:barb|bob|barbe|bard)\b", "Bee and Barb"),
    # people (vanilla Whiterun hold)
    (r"\bbal\s*(?:gr|g)?(?:roof|ruf|rough|groove|grove)\b|\bbulgruf\b|\bball\s+groove\b", "Balgruuf"),
    (r"\b(?:i\s+rel?\s*eth|irilith|ira\s+leth|ear\s+a\s+leth)\b", "Irileth"),
    (r"\b(?:far\s+in\s+gar|ferengar|farren\s*gar|fairen\s*gar)\b", "Farengar"),
    (r"\b(?:gertrude|gerda|gird\s+ur|gerder|gerdure)\b", "Gerdur"),
    (r"\b(?:fendel|fendal|fandal|fan\s+dal)\b", "Faendal"),
    (r"\b(?:elvor|al\s+vore|alvar)\b", "Alvor"),
    (r"\b(?:had\s+far|hadvard|had\s+var)\b", "Hadvar"),
    (r"\b(?:ralph|rolf|rah\s+loaf)\b", "Ralof"),
    (r"\b(?:del\s+fine|delfine)\b", "Delphine"),
    (r"\b(?:kai'?s|kaius|kyus|keyes)\b", "Caius"),
    (r"\b(?:emar|aimar|eye\s+mar)\b", "Eimar"),
    (r"\b(?:ulf\s+rick|olfrick|alfrick|elf\s+rick|ulf\s+rich)\b", "Ulfric"),
    (r"\b(?:storm\s+cloaks?|storm\s+clocks?)\b", "Stormcloak"),
    (r"\b(?:ma'?avin|ma'?aven(?:ed)?|mavin|may\s+ven|maven'?ed)\b", "Maven"),
    (r"\bblack\s+(?:briar|brier|bryer|friar)\b", "Black-Briar"),
    (r"\b(?:brinjolf|bryn\s+jolf|brin\s+yolf|brynjulf)\b", "Brynjolf"),
    (r"\b(?:named|called|for|find|is|about)\s+(?:vault|wald|valde|valled|valid)\b", "... Vald"),
    # lore words
    (r"\bdragon\s+born\b", "Dragonborn"),
    (r"\b(?:dova\s*keen|dovakin|dove\s+a\s+keen|dover\s+keen|dova\s+kin)\b", "Dovahkiin"),
    (r"\b(?:septums?|sceptims?|sept\s+ims?)\b", "septims"),
    (r"\b(?:thieves\s+guilt|thieves\s+gild)\b", "Thieves Guild"),
    (r"\b(?:companion's|companions)\s+(?:hall|whole)\b|\bjorrvaskr\b|\byour\s+vask(?:er|ur)\b", "Jorrvaskr"),
    (r"\b(?:the\s+grey|the\s+gray)\s*beards?\b|\bgraybeards\b", "Greybeards"),
    (r"\bthu'?um\b|\btoo\s+um\b", "Thu'um"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), meant) for p, meant in MISHEARD]

# Words that look like a name to a fuzzy match but are almost always meant as themselves
_COMMON = {
    "seven", "even", "heaven", "never", "ever", "over", "other", "older", "order", "hold", "hello", "help",
    "there", "their", "where", "here", "here's", "were", "with", "what", "when", "then", "than", "that",
    "this", "they", "them", "these", "those", "your", "you're", "yours", "would", "could", "should",
    "about", "again", "alone", "along", "also", "always", "around", "because", "before", "being", "below",
    "better", "brother", "father", "mother", "sister", "daughter", "lady", "lord", "sorry", "thanks",
    "maybe", "money", "mead", "dead", "deed", "need", "sword", "word", "world", "worth", "dragon", "dragons",
    "right", "night", "light", "fight", "might", "sight", "tight", "great", "guard", "guards", "going",
}


def hearing_enabled() -> bool:
    return os.environ.get(HEARING_ENV, "").strip().lower() not in _OFF


def prompt_lines(npc_names: Iterable[str], player_name: str) -> list[str]:
    """The standing rule, once per prompt, for every NPC in it."""
    names = [n for n in npc_names if n]
    who = names[0] if len(names) == 1 else "those listening"
    return [
        f"{player_name} speaks aloud, and what {who} hear{'s' if len(names) == 1 else ''} can come through "
        "garbled, the way half-heard words do: a wrong word that sounds like the right one "
        "(\"stain of white run\" for \"thane of Whiterun\", \"Kai's\" for \"Caius\").",
        f"Most lines from {player_name} come through clearly: answer those normally and never question a word "
        "that fits.",
        f"Only if a line makes no sense, or a key word seems wrong, do not answer it literally and do not "
        "pretend to understand. Repeat back the word you think they meant as a short question, in character, "
        "or ask them to say it again. Never ask about a word they did not say.",
        "If the meaning is clear despite a wrong word, just answer what they meant without remarking on it.",
        "A note in the conversation may name likely misheard words. Use it as a guess, not a fact.",
        "Never mention transcription, microphones or text; to you it is only speech, sometimes unclear.",
    ]


def notes(text: str, names: Iterable[str] = ()) -> list[str]:
    """Likely mishearings in one player line, as short '"heard" may be "meant"' notes. Empty when none."""
    if not text or not text.strip():
        return []
    found: list[tuple[str, str]] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern, meant in _COMPILED:
        for m in pattern.finditer(text):
            heard = m.group(0).strip()
            if meant == "thane of ...":
                hold = re.split(r"\s+of\s+", heard, maxsplit=1, flags=re.IGNORECASE)[1]
                meant_here = f"thane of {_hold_name(hold)}"
            elif meant.startswith("... "):
                # the pattern needs the word before the name for context; keep that word as heard
                meant_here = heard.rsplit(None, 1)[0] + " " + meant[4:]
            else:
                meant_here = meant
            if _norm(heard) == _norm(meant_here):
                continue
            if any(m.start() < e and s < m.end() for s, e in seen_spans):
                continue
            seen_spans.append((m.start(), m.end()))
            found.append((heard, meant_here))
    found.extend(_name_near_misses(text, names, seen_spans))
    return [f'"{heard}" may be "{meant}"' for heard, meant in found]


def note_text(text: str, names: Iterable[str], player_name: str) -> str | None:
    """One narration line for the player's message, or None."""
    found = notes(text, names)
    if not found:
        return None
    return f"{player_name}'s words were hard to make out; possibly misheard: " + "; ".join(found) + "."


_HOLD_NAMES = {
    "whiterun": "Whiterun", "whiterum": "Whiterun", "riften": "Riften", "riftin": "Riften", "solitude": "Solitude",
    "windhelm": "Windhelm", "markarth": "Markarth", "falkreath": "Falkreath", "fallcreeth": "Falkreath",
    "falcrest": "Falkreath", "morthal": "Morthal", "dawnstar": "Dawnstar", "winterhold": "Winterhold",
    "thereach": "the Reach", "thepale": "the Pale", "therift": "the Rift", "hjaalmarch": "Hjaalmarch",
    "eastmarch": "Eastmarch", "haafingar": "Haafingar",
}


def _hold_name(heard: str) -> str:
    return _HOLD_NAMES.get(re.sub(r"[^a-z]", "", heard.lower()), heard)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _name_near_misses(text: str, names: Iterable[str], taken: list[tuple[int, int]]) -> list[tuple[str, str]]:
    """Words close to, but not, the name of someone in the scene ("Gerder" for "Gerdur")."""
    first_names: dict[str, str] = {}
    for name in names:
        for part in re.findall(r"[A-Za-z']+", name or ""):
            if len(part) >= 4 and part.lower() not in _COMMON:
                first_names.setdefault(part.lower(), part)
    if not first_names:
        return []
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"[A-Za-z']+", text):
        word = m.group(0).lower().strip("'")
        if word.endswith("'s"):
            word = word[:-2]
        if len(word) < 4 or word in first_names or word in _COMMON:
            continue
        if any(m.start() < e and s < m.end() for s, e in taken):
            continue
        close = difflib.get_close_matches(word, list(first_names), n=1, cutoff=0.8)
        if close:
            out.append((m.group(0), first_names[close[0]]))
    return out
