"""Living World work: NPC tasks and shared work (W1, server side).

People with a real need (Gerdur's mill needs firewood before winter) open a task, offer it to someone
they trust, and remember how it went. A task's life is the eight states in ``tasks.STATES``; every step
is a ``task.*`` world event plus a ``task_change`` row, so it rides the save timeline and the shared event
log. Tasks change relationships only through ``relationship_change`` rows and gossip only through claims
(``social/claims.py``); this package never touches romance tables or the attraction dimension.

No LLM calls: needs, offers, deadlines and consequences are rules. The NPC's own words come from the
prompt lines in ``prompts.py`` and the AgreeTask / CheckTask / ReleaseTask actions.

On with Living World unless ``MANTELLA_LW_TASKS=0``. Children never give, take or witness tasks.
"""
from __future__ import annotations

import os

TASKS_ENV = "MANTELLA_LW_TASKS"


def tasks_enabled() -> bool:
    return os.environ.get(TASKS_ENV, "").strip().lower() not in {"0", "false", "no", "off"}
