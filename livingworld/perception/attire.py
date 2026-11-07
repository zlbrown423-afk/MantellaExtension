"""Clothing awareness: noticing who has no clothes or armor on their body, and remembering it.

The game sends what each nearby person wears in the body slot (see ``records.py``, type ``attire``).
Only changes are stored, as ``world_event`` rows of type ``attire`` on the active save timeline:

* someone is seen with nothing in the body slot: one event, a memory for each NPC who could see
  them (line of sight, within sight range), and a memory for the NPC themself;
* while they stay that way, NPCs who arrive and see them get the same event and a memory, once;
* they dress again: one event that closes the state, no memories.

Someone never seen before counts as dressed. This is perception only. Nothing here changes a
relationship, and nothing here changes what anyone is willing to do.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from livingworld.perception import formation, intoxication
from livingworld.perception.formation import Bond, MemoryDraft, Perception
from livingworld.perception.records import PerceptionRecord

EVENT_TYPE = "attire"
UNCOVERED_IMPORTANCE = 0.45   # just above recall's threshold, so an NPC may bring it up unprompted


@dataclass
class AttireState:
    event_id: int
    covered: bool
    game_time: float


def last_state(db, actor_id: int) -> AttireState | None:
    """The actor's latest attire change on the active timeline."""
    row = db.execute(
        "SELECT event_id, json_extract(payload_json, '$.covered'), game_time FROM v_visible_event"
        " WHERE type = ? AND json_extract(actors_json, '$[0]') = ? ORDER BY game_time DESC, event_id DESC LIMIT 1",
        (EVENT_TYPE, actor_id)).fetchone()
    return AttireState(row[0], bool(row[1]), row[2]) if row else None


def is_covered(record: PerceptionRecord) -> bool:
    return record.cover != "none"


def _place(record: PerceptionRecord) -> str:
    return f" in {record.location}" if record.location else ""


def uncovered_for_witness(record: PerceptionRecord, seen: Perception, to_actor: Bond) -> MemoryDraft:
    actor = record.actor.name
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    if to_actor.close:
        interpretation = f"Strange to see {actor} like that. I hope nothing is wrong."
        valence = -0.1
    else:
        interpretation = f"I don't know why {actor} was going about like that. Odd."
        valence = -0.15
    return MemoryDraft(
        content=f"{hedge} {actor} with no clothes or armor on, only underclothes{_place(record)}.",
        interpretation=interpretation, importance=formation._clamp(UNCOVERED_IMPORTANCE * (0.7 + 0.3 * seen.confidence)),
        valence=valence, intensity=0.3, confidence=seen.confidence, source_kind="witnessed", deltas_to_actor={})


def uncovered_for_self(record: PerceptionRecord, seen_by: list[str]) -> MemoryDraft:
    if seen_by:
        names = seen_by[0] if len(seen_by) == 1 else ", ".join(seen_by[:-1]) + " and " + seen_by[-1]
        who = f" {names} saw me."
    else:
        who = ""
    return MemoryDraft(content=f"I had no clothes or armor on, only underclothes{_place(record)}.{who}",
                       interpretation=None, importance=0.4, valence=-0.1, intensity=0.3, confidence=1.0,
                       source_kind="experienced", deltas_to_actor={})


class AttireTracker:
    """Turns attire snapshots into state changes, witnesses and memories (called by ``PerceptionPipeline``)."""

    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline
        self.store = pipeline.store
        self.identities = pipeline.identities

    def record(self, record: PerceptionRecord):
        from livingworld.perception.pipeline import Outcome
        gt = record.game_time
        actor_id = self.identities.ensure_npc(record.actor.identity(), gt)
        if self.pipeline._child(actor_id):
            return Outcome(None)                              # children are left out entirely
        covered = is_covered(record)
        seers: dict[int, tuple[str, Perception]] = {}
        unseen = []
        for witness in record.witnesses:
            npc_id = self.identities.ensure_npc(witness.actor.identity(), gt)
            if npc_id == actor_id or self.pipeline._child(npc_id):
                continue
            seen = formation.perceives(witness)
            if seen is None:
                unseen.append(witness.actor.name)
            else:
                seers[npc_id] = (witness.actor.name, seen)

        timeline = self.store.active_timeline_id()
        with self.store.transaction() as db:
            previous = last_state(db, actor_id)
            was_covered = previous is None or previous.covered
            if covered and was_covered:
                return Outcome(None, duplicate=True)          # nothing changed
            if covered or was_covered:
                event_id = db.execute(
                    "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
                    " game_key, real_time, confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (timeline, EVENT_TYPE, record.location, json.dumps([actor_id]),
                     json.dumps({"covered": covered, "cover": record.cover, "body": record.body,
                                 "nearby_but_unseen": unseen}),
                     "game", gt, record.key, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 1.0)
                ).lastrowid
                new_state = True
            else:
                event_id = previous.event_id                  # still undressed: add whoever newly sees it
                new_state = False
            outcome = Outcome(event_id, duplicate=not new_state)
            if covered:
                return outcome

            already = {row[0] for row in db.execute("SELECT npc_id FROM event_witness WHERE event_id = ?", (event_id,))}
            if new_state:
                db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
                           (event_id, actor_id, "participated"))
                outcome.witnesses[actor_id] = "participated"
            new_seers = {i: v for i, v in seers.items() if i not in already}
            for npc_id, (_, seen) in new_seers.items():
                db.execute("INSERT INTO event_witness (event_id, npc_id, how, distance_m, had_los) VALUES (?,?,?,?,?)",
                           (event_id, npc_id, seen.how, seen.distance_m, 1))
                outcome.witnesses[npc_id] = seen.how

            drafts: dict[int, MemoryDraft] = {}
            if new_state and not self.pipeline._is_player(db, actor_id):
                drafts[actor_id] = uncovered_for_self(record, [name for name, _ in seers.values()])
            for npc_id, (_, seen) in new_seers.items():
                if not self.pipeline._is_player(db, npc_id):
                    drafts[npc_id] = uncovered_for_witness(record, seen, self.pipeline._bond(db, npc_id, actor_id))
            for npc_id, draft in drafts.items():
                intoxication.hazy(draft, intoxication.drink_state(db, npc_id, gt).stage)
                about = [] if npc_id == actor_id else [actor_id]
                mem_id = db.execute(
                    "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                    " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (timeline, npc_id, "episodic", draft.content, json.dumps(about), record.location, gt,
                     draft.importance, draft.valence, draft.intensity, "perception", draft.source_kind,
                     draft.interpretation, draft.confidence, event_id)).lastrowid
                db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
                outcome.memories[npc_id] = mem_id
            outcome.unseen = unseen
        return outcome
