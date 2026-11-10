"""One more extraction for past conversations that left an NPC no memory of what was said.

Before MANTELLA_LW_MEMORY_BOOST, extraction replies were cut off by the spoken-reply cap (max_tokens 250)
or had their memories dropped, so most conversations taught the NPCs nothing. This queues those
conversations once more (each one only ever once: the job key is fixed). Backfilled jobs add memories
and facts but no gossip reports, and never replace a fact learned later.

MANTELLA_LW_MEMORY_BACKFILL=0 skips it; MANTELLA_LW_MEMORY_BACKFILL_MAX sets how many of the most recent
conversations are looked at (default 60).
"""
from __future__ import annotations

import os

from livingworld.memory.jobs import JobQueue
from livingworld.memory.settings import memory_boost_enabled
from livingworld.store.world_store import WorldStore

BACKFILL_ENV = "MANTELLA_LW_MEMORY_BACKFILL"
BACKFILL_MAX_ENV = "MANTELLA_LW_MEMORY_BACKFILL_MAX"
DEFAULT_MAX = 60
MIN_LINES = 4
EXTRACT_JOB = "extract_conversation"


def backfill_enabled() -> bool:
    return memory_boost_enabled() and os.environ.get(BACKFILL_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _max() -> int:
    try:
        return max(0, int(os.environ.get(BACKFILL_MAX_ENV, "") or DEFAULT_MAX))
    except ValueError:
        return DEFAULT_MAX


def queue_backfill(store: WorldStore) -> int:
    if not backfill_enabled():
        return 0
    rows = store.query(
        "SELECT c.conv_id FROM conversation c"
        " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt"
        " WHERE c.ended_gt IS NOT NULL"
        " AND (SELECT COUNT(*) FROM utterance u WHERE u.conv_id = c.conv_id) >= ?"
        " AND NOT EXISTS (SELECT 1 FROM memory m JOIN memory_source ms ON ms.mem_id = m.mem_id"
        "                 JOIN utterance u ON u.utt_id = ms.utt_id"
        "                 WHERE u.conv_id = c.conv_id AND m.created_by = 'extraction')"
        " AND EXISTS (SELECT 1 FROM job j WHERE j.idempotency_key = 'extract:conv:' || c.conv_id"
        "             AND j.status IN ('done', 'dead'))"
        " ORDER BY c.started_gt DESC LIMIT ?", (MIN_LINES, _max()))
    queue = JobQueue(store)
    return sum(queue.enqueue(EXTRACT_JOB, {"conv_id": conv_id, "backfill": True}, f"extract:backfill:conv:{conv_id}")
               for (conv_id,) in rows)
