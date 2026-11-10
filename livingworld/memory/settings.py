"""Switches for the memory improvements (better extraction and recall when a conversation starts again).

MANTELLA_LW_MEMORY_BOOST=0 turns them off and brings back the old behaviour: extraction then uses the
spoken-reply settings (max_tokens, stop), and recall shows only the newest memories with no recap of
the last conversation.
MANTELLA_LW_EXTRACT_MAX_TOKENS sets how long an extraction reply may be (default 1500).
"""
from __future__ import annotations

import os

BOOST_ENV = "MANTELLA_LW_MEMORY_BOOST"
EXTRACT_TOKENS_ENV = "MANTELLA_LW_EXTRACT_MAX_TOKENS"
DEFAULT_EXTRACT_TOKENS = 1500
_OFF = {"0", "false", "no", "off"}


def memory_boost_enabled() -> bool:
    return os.environ.get(BOOST_ENV, "").strip().lower() not in _OFF


def extract_max_tokens() -> int:
    try:
        return max(400, int(os.environ.get(EXTRACT_TOKENS_ENV, "") or DEFAULT_EXTRACT_TOKENS))
    except ValueError:
        return DEFAULT_EXTRACT_TOKENS
