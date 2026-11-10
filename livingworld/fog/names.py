"""Text rules for names: what a person is called in speech, and who said whose name in a line.

NPC lines come from the language model, so names in them are spelled and capitalised right; matching
there is case-sensitive. Player lines come through speech-to-text, which often lowercases names, so
matching there ignores case.
"""
from __future__ import annotations

import re
from typing import Iterable

# Words before or inside a name that people do not go by on their own
TITLES = {"jarl", "thane", "housecarl", "steward", "court", "wizard", "captain", "commander", "general",
          "legate", "high", "king", "queen", "lord", "lady", "sir", "brother", "sister", "mother", "father",
          "old", "young", "the", "of", "and", "dark", "elder", "master", "priest", "priestess", "keeper"}

# Words the player might say after "call me" that are not a name
NOT_A_NAME = {"a", "an", "the", "what", "whatever", "anything", "anyone", "whenever", "when", "if", "back", "later",
              "sir", "madam", "maybe", "please", "now", "soon", "tomorrow", "that", "it", "this", "crazy", "mad",
              "friend", "stranger", "nobody", "nothing", "just", "here", "there", "your", "my", "his", "her",
              "not", "no", "yes", "so", "too", "only", "really", "very", "in", "on", "at", "by", "for", "to",
              "tired", "sorry", "fine", "good", "okay", "ok", "lost", "new", "looking", "going", "trying",
              "from", "with", "about", "afraid", "glad", "here", "busy", "done", "sure"}


def call_names(display_name: str) -> list[str]:
    """The names someone answers to, longest first: 'Jarl Balgruuf the Greater' -> ['Jarl Balgruuf the
    Greater', 'Balgruuf']; 'Lucan Valerius' -> ['Lucan Valerius', 'Lucan']."""
    full = " ".join(display_name.split())
    if not full:
        return []
    out = [full]
    for word in re.findall(r"[A-Za-z][A-Za-z'\-]*", full):
        if word.lower() in TITLES:
            continue
        if len(word) >= 3 and word not in out:
            out.append(word)
        break
    return out


def _pattern(names: Iterable[str]) -> str:
    alts = sorted({re.escape(n) for n in names if n}, key=len, reverse=True)
    return r"(?<![A-Za-z'\-])(?:" + "|".join(alts) + r")(?![A-Za-z\-])" if alts else r"(?!x)x"


def says_name(text: str, display_name: str, ignore_case: bool = False) -> bool:
    """Whether ``text`` says the person's name (or the short name they go by)."""
    names = call_names(display_name)
    if not names or not text:
        return False
    return re.search(_pattern(names), text, re.IGNORECASE if ignore_case else 0) is not None


_INTRO = re.compile(
    r"\b(?:my\s+name\s+is|my\s+name's|the\s+name's|the\s+name\s+is|name's|i\s+am\s+called|i'm\s+called|"
    r"they\s+call\s+me|you\s+can\s+call\s+me|just\s+call\s+me|call\s+me|i\s+go\s+by|people\s+call\s+me)"
    r"\s+([A-Za-z][A-Za-z'\-]*)", re.IGNORECASE)
_I_AM = re.compile(r"\b(?:i'm|i\s+am|it's|it\s+is|this\s+is)\s+([A-Za-z][A-Za-z'\-]*)", re.IGNORECASE)


def player_introduction(text: str, player_name: str) -> str | None:
    """The name the player gives for themselves in this line, or None.

    'My name is Zach', 'call me Wolf', 'the name's Zach' give whatever name follows. 'I'm Zach' counts only
    with the player's own name, since 'I'm tired' is not an introduction. A name that matches the player's
    own comes back spelled as the player's name.
    """
    if not text:
        return None
    own = call_names(player_name)
    match = _INTRO.search(text)
    if match:
        word = match.group(1)
        if word.lower() in NOT_A_NAME:
            return None
        for name in own:
            if word.lower() == name.lower() or word.lower() == name.split()[0].lower():
                return own[-1] if len(own) > 1 else own[0]
        return word[:1].upper() + word[1:]
    for m in _I_AM.finditer(text):
        word = m.group(1).lower()
        for name in own:
            if word == name.lower() or word == name.split()[0].lower():
                return own[-1] if len(own) > 1 else own[0]
    return None
