"""Lines that start with "debug" are test commands for the Living World, not speech.

They still run (``debug guard reinstate``, ``debug max dirt``...), but nobody hears them: the NPC is shown a neutral
stand-in instead of the words, and the line is kept out of the conversation log the memories, facts, leads and
diaries are made from, and out of Mantella's summary. Off with ``MANTELLA_LW_HIDE_DEBUG=0`` (the old behaviour:
the words go to the NPC and are remembered like anything else said).

Speech recognition often mangles a spoken command (log 2026-11-17: "The Bug Guard Reinstate", "Debug guard
re-in-state"). :func:`command_text` turns such a line back into the plain typed form ("debug guard reinstate"):

* a misheard "debug" at the start ("the bug", "de bug", "debugg", "d bug"...) counts, but ONLY when the very next
  word names a command family (guard, face, profile, max dirt, clean, filthy) AND the whole line then parses as a
  known command, so ordinary speech that happens to start with "the bug" is left alone;
* hyphens and spaces inside command words are closed up ("re-in-state", "re in state" -> "reinstate",
  "pro mote" -> "promote"), only where the joined word is a known command word.

Off with ``MANTELLA_LW_DEBUG_FUZZY=0`` (only lines typed or heard as "debug ..." count, word for word as before).
"""
from __future__ import annotations

import os
import re

ENV = "MANTELLA_LW_HIDE_DEBUG"
FUZZY_ENV = "MANTELLA_LW_DEBUG_FUZZY"
_OFF = {"0", "false", "no", "off"}
_DEBUG = re.compile(r"^\W*debug\b", re.I)

STAND_IN = ("(The player pauses for a moment to see to something and says nothing. Do not mention it or ask about "
            "it; just carry on, in one short line, as if nothing was said.)")

# How speech recognition writes a spoken "debug" at the start of a line (as words, lower case, no punctuation).
_PREFIXES = (("debug",), ("debugg",), ("debuggy",), ("debugs",), ("debugged",), ("dbug",), ("debog",),
             ("de", "bug"), ("dee", "bug"), ("d", "bug"), ("the", "bug"), ("da", "bug"), ("duh", "bug"),
             ("de", "bugg"), ("the", "bugs"), ("de", "bugs"), ("deeb", "bug"))
# The word right after the prefix must start one of the command families, so "the bug bit me" is speech.
_FAMILIES = {"guard", "face", "profile", "max", "maximum", "clean", "filthy"}
_ALIASES = {"guards": "guard", "gard": "guard", "garde": "guard"}
_MAX_JOIN = 4                 # "re in state" is three pieces; four covers "pro ba tion"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def fuzzy_enabled() -> bool:
    return os.environ.get(FUZZY_ENV, "").strip().lower() not in _OFF


def _vocabulary() -> set[str]:
    from livingworld.guard import debug as guard_debug
    from livingworld.guard import orgs as O
    from livingworld.personality import debug as personality_debug
    from livingworld.bodylang import face_test
    words = set(guard_debug.COMMANDS) | set(personality_debug.COMMANDS) | set(face_test.MOODS)
    words |= _FAMILIES | {"dirt", "dirty", "camp", "road", "brawl", "theft"}
    for o in O.orgs():
        words |= {o.hold.lower(), o.key.split("_")[0]} | {r.key for r in o.ranks}
        words |= {w for r in o.ranks for w in r.title.lower().split()}
    return words


def _join_pieces(words: list[str], vocab: set[str]) -> list[str]:
    """Close up command words that were split ("re in state" -> "reinstate"), longest join first."""
    out: list[str] = []
    i = 0
    while i < len(words):
        for n in range(min(_MAX_JOIN, len(words) - i), 1, -1):
            joined = "".join(words[i:i + n])
            if joined in vocab and not all(w in vocab for w in words[i:i + n]):
                out.append(joined)
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def _parses(line: str) -> bool:
    """True when ``line`` is a known test command (guard, hygiene, face or profile)."""
    from livingworld.guard import debug as guard_debug
    from livingworld.hygiene import debug as hygiene_debug
    from livingworld.personality import debug as personality_debug
    from livingworld.bodylang import face_test
    return any(p(line) is not None for p in (guard_debug.parse, hygiene_debug.parse, face_test.parse,
                                             personality_debug.parse))


def command_text(text: str | None) -> str | None:
    """The line as a typed test command ("debug guard reinstate"), or None when it is not one.

    A line that already starts with the word "debug" always counts (as before, even if the rest is unknown) and
    comes back with split command words closed up. Misheard prefixes count only for a known command."""
    if not text:
        return None
    exact = bool(_DEBUG.match(text))
    if not fuzzy_enabled():
        return text if exact else None
    # hyphens inside words are just how the recogniser spelled it: "re-in-state" -> "re in state" (joined below)
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    prefix = next((p for p in sorted(_PREFIXES, key=len, reverse=True) if tuple(words[:len(p)]) == p), None)
    if prefix is None:
        return text if exact else None
    vocab = _vocabulary()
    rest = _join_pieces([_ALIASES.get(w, w) for w in words[len(prefix):]], vocab)
    line = " ".join(["debug", *rest])
    if exact:   # unchanged words keep the original line (names keep their case); a repaired one must parse
        return text if rest == words[len(prefix):] else (line if _parses(line) else text)
    if not rest or rest[0] not in _FAMILIES or not _parses(line):
        return None
    return line


def is_debug(text: str | None) -> bool:
    return bool(text) and enabled() and command_text(text) is not None
