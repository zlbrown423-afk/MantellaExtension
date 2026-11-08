"""Test commands for guard careers, said (or typed) to an NPC during a conversation.

* "debug guard enlist" / "debug guard enlist whiterun" - join that hold's guard at once (as an admin change)
* "debug guard rank optio"                              - set the player's rank in the guard they serve in
* "debug guard dismiss" / "debug guard reinstate"
* "debug guard thane" / "debug guard thane whiterun"    - count the player as thane of that hold from now on
* "debug guard status"                                  - write the player's record to the server log

"debug" (or "test") and "guard" must both be in the line, so normal speech never triggers it. The hold
defaults to the hold of a guard leader or guard in the conversation, else the first configured hold.
Switch off with MANTELLA_LW_GUARD_DEBUG=0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from livingworld.guard import orgs as O

COMMANDS = ("enlist", "rank", "dismiss", "reinstate", "thane", "status")


@dataclass(frozen=True)
class Command:
    verb: str
    org_key: str | None
    rank: str | None = None


def parse(text: str) -> Command | None:
    words = re.findall(r"[a-z]+", (text or "").lower())
    joined = " ".join(words)
    if not re.search(r"\b(debug|test)\b", joined) or "guard" not in words:
        return None
    verb = next((w for w in words if w in COMMANDS), None)
    if verb is None:
        return None
    org = next((o.key for o in O.orgs() for w in words if w in (o.hold.lower(), o.key.split("_")[0])), None)
    rank = None
    if verb == "rank":
        keys = {r.key for o in O.orgs() for r in o.ranks} | {r.title.lower() for o in O.orgs() for r in o.ranks}
        rank = next((w for w in words[words.index("rank") + 1:] if w in keys), None)
        if rank is None:
            return None
    return Command(verb, org, rank)
