"""Test and inspect commands, said (or typed) to an NPC during a conversation. "debug" (or "test") and "profile"
must both be in the line, so normal speech never triggers them.

* "debug profile"            - write each woman's full profile (values, sources, change history, what the player
                               knows) to the server log and to data/livingworld/personality-inspect/<name>.json
* "debug profile known"      - only what the player has found out (the player's view)
* "debug profile evolve"     - run change over time now
* "debug profile reset"      - drop her change over time (back to the generated starting point)
* "debug profile regenerate" - generate her starting point again, and drop change over time

Switch off with MANTELLA_LW_PERSONALITY_DEBUG=0.
"""
from __future__ import annotations

import os
import re

COMMANDS = ("known", "evolve", "reset", "regenerate")
ENV = "MANTELLA_LW_PERSONALITY_DEBUG"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def parse(text: str) -> str | None:
    words = re.findall(r"[a-z]+", (text or "").lower())
    if not ({"debug", "test"} & set(words)) or "profile" not in words:
        return None
    return next((w for w in words[words.index("profile") + 1:] if w in COMMANDS), "inspect")
