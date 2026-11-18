"""Drop Whisper's echo of its own hint from what the player said.

Mantella gives Whisper a hint before each turn ("This is a conversation with Hulda in The Bannered Mare.")
so it spells names right. When the mic only hears breath, room noise or a click, Whisper often writes
the hint back out as if the player had said it. That line then shows in the log and the [Mic] HUD, goes
to the NPC as the player's words, and ends their wait for the player. This module removes it.

Only sentences that START with the hint's first words are touched, so a player who really says
"this is ..." is unaffected unless they recite the hint itself. Off: MANTELLA_LW_PROMPT_ECHO=0.
"""
from __future__ import annotations

import os
import re

_WORD = re.compile(r"[\w']+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MIN_MATCH = 5   # "this is a conversation with" - fewer words could be real speech


def enabled() -> bool:
    return os.environ.get("MANTELLA_LW_PROMPT_ECHO", "1").strip() != "0"


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _strip_sentence(sentence: str, hint: list[str]) -> str:
    """The sentence without a leading copy of the hint (all of it, or its first MIN_MATCH+ words)."""
    spans = list(_WORD.finditer(sentence))
    matched = 0
    while matched < len(spans) and matched < len(hint) and spans[matched].group(0).lower() == hint[matched]:
        matched += 1
    if matched < min(MIN_MATCH, len(hint)):
        return sentence
    if matched < len(spans) and matched < len(hint):
        # The echo went off the hint part way ("...with Hulda in Whiterun"): the rest of the sentence is
        # the echo's tail, not speech.
        return ""
    return sentence[spans[matched - 1].end():].lstrip(" ,.;:!?-").strip()


def strip_prompt_echo(text: str, prompt: str) -> str:
    """``text`` without any sentence that is Whisper repeating ``prompt``; '' when nothing else is left."""
    if not text or not prompt or not enabled():
        return text
    hint = _words(prompt)
    if len(hint) < MIN_MATCH:
        return text
    kept = []
    for sentence in _SENTENCE_END.split(text.strip()):
        rest = _strip_sentence(sentence, hint)
        if rest:
            kept.append(rest)
    return " ".join(kept).strip()
