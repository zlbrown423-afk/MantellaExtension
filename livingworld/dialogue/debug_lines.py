"""Lines that start with "debug" are test commands for the Living World, not speech.

They still run (``debug guard reinstate``, ``debug max dirt``...), but nobody hears them: the NPC is shown a neutral
stand-in instead of the words, and the line is kept out of the conversation log the memories, facts, leads and
diaries are made from, and out of Mantella's summary. Off with ``MANTELLA_LW_HIDE_DEBUG=0`` (the old behaviour:
the words go to the NPC and are remembered like anything else said)."""
from __future__ import annotations

import os
import re

ENV = "MANTELLA_LW_HIDE_DEBUG"
_DEBUG = re.compile(r"^\W*debug\b", re.I)

STAND_IN = ("(The player pauses for a moment to see to something and says nothing. Do not mention it or ask about "
            "it; just carry on, in one short line, as if nothing was said.)")


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def is_debug(text: str | None) -> bool:
    return bool(text) and enabled() and bool(_DEBUG.match(text))
