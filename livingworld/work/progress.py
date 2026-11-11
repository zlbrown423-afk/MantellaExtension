"""Proof of work: game events that move a task forward. Runs as the ``work`` consumer of the shared
event processor (``social/processor.py``), so each event counts exactly once.

Event types read (written by the game side in W2, or by the developer tool):

- ``item_given``: actors [giver of the items, recipient], payload {"item", "count"}. Counts toward an
  open task the recipient gave the giver of the items, when the item matches and the template counts
  ``given``.
- ``work_done``: actors [worker], payload {"item", "count", "furniture"}. The worker used the work spot
  (e.g. split logs at a chopping block). Starts the task (in progress) and counts toward it only when
  the template counts ``worked``; otherwise the result still has to be handed over.

Reaching the needed amount completes the task, with its consequences.
"""
from __future__ import annotations

import json

from livingworld.work import consequences, tasks as T, tasks_enabled
from livingworld.work.templates import by_key


def _matches(task: T.Task, item: str | None) -> bool:
    """``need_item`` may list several items: 'Leek|Wheat|Gourd'."""
    if not task.need_item or not item:
        return False
    return any(want.strip() and want.strip().lower() in item.lower() for want in task.need_item.split("|"))


def consume(db, event) -> None:
    etype = event["type"]
    if etype not in ("item_given", "work_done") or not tasks_enabled():
        return
    payload = json.loads(event["payload_json"] or "{}")
    actors = json.loads(event["actors_json"] or "[]")
    if not actors:
        return
    worker = actors[0]
    count = float(payload.get("count") or 1)
    gt = event["game_time"]
    for task in T.taken_by(db, worker):
        t = by_key(task.template)
        counts = t.evidence if t else ("given",)
        if not _matches(task, payload.get("item")):
            continue
        if etype == "item_given":
            if len(actors) < 2 or actors[1] != task.giver_id or "given" not in counts:
                continue
            reason = f"gave {count:g} {payload.get('item')}"
        else:
            reason = f"worked: {count:g} {payload.get('item')}" + (
                f" at the {payload['furniture']}" if payload.get("furniture") else "")
            if "worked" not in counts:
                if task.state == "accepted":
                    T.change(db, task, "in_progress", reason, gt, cause_event_id=event["event_id"])
                continue
        state = "in_progress" if task.state in ("accepted", "paused") else None
        task = T.change(db, task, state, reason, gt, d_progress=count, cause_event_id=event["event_id"])
        if task.done_enough:
            consequences.completed(db, task, gt, f"all {task.need_count} {task.units} done")
        return   # one task takes one event
