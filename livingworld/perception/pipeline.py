"""Stores perceived events: one objective ``world_event`` row, the people who perceived it in
``event_witness``, a separate first-person ``memory`` for each NPC, and small relationship changes.

Everything is written on the active save timeline, so loading an older save hides it again.
An event the game sends twice (same key on the same timeline) is stored once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src import utils

from livingworld.perception import formation, intoxication
from livingworld.perception.formation import Bond, MemoryDraft, Perception
from livingworld.perception.records import (BadRecord, PerceptionRecord, body_part, item_category, parse_record,
                                            status_for_rank)
from livingworld.store.identity import IdentityRegistry
from livingworld.store.world_store import WorldStore

logger = utils.get_logger()


@dataclass
class Outcome:
    event_id: int | None
    duplicate: bool = False
    memories: dict[int, int] = field(default_factory=dict)   # npc_id -> mem_id
    witnesses: dict[int, str] = field(default_factory=dict)  # npc_id -> how
    unseen: list[str] = field(default_factory=list)          # nearby names who did not perceive it


class PerceptionPipeline:
    def __init__(self, store: WorldStore) -> None:
        self.store = store
        self.identities = IdentityRegistry(store)

    def ingest(self, raw_records: list[str]) -> list[Outcome]:
        """Parse and store each record; a bad record is logged and skipped, never fatal."""
        outcomes = []
        for raw in raw_records:
            try:
                record = parse_record(raw)
            except (BadRecord, ValueError) as e:
                logger.error(f"Living World: perception record skipped ({e}): {raw[:200]}")
                continue
            outcomes.append(self.record(record))
        return outcomes

    def record(self, record: PerceptionRecord) -> Outcome:
        if record.type == "attire":
            from livingworld.perception.attire import AttireTracker
            return AttireTracker(self).record(record)
        timeline = self.store.active_timeline_id()
        existing = self.store.scalar("SELECT event_id FROM world_event WHERE timeline_id = ? AND game_key = ?",
                                     (timeline, record.key))
        if existing is not None:
            return Outcome(existing, duplicate=True)

        gt = record.game_time
        actor_id = self.identities.ensure_npc(record.actor.identity(), gt)
        target_id = self.identities.ensure_npc(record.target.identity(), gt) if record.target else None
        people: dict[int, tuple[str, Perception]] = {}   # npc_id -> (role, perception)
        people[actor_id] = ("actor", Perception("participated", "experienced", 1.0))
        if target_id is not None:
            people[target_id] = ("target", Perception("participated", "experienced", 1.0))
        unseen = []
        witness_ids = []
        for witness in record.witnesses:
            npc_id = self.identities.ensure_npc(witness.actor.identity(), gt)
            witness_ids.append((npc_id, witness))
            if npc_id in people:
                continue
            seen = formation.perceives(witness)
            if seen is None:
                unseen.append(witness.actor.name)
                continue
            people[npc_id] = ("witness", seen)

        outcome = Outcome(None, unseen=unseen)
        with self.store.transaction() as db:
            # the game's own relationship ranks seed relationship rows that do not exist yet
            if target_id is not None:
                self._seed_bond(db, target_id, actor_id, record.target_rank, gt)
            for npc_id, witness in witness_ids:
                if people.get(npc_id, ("",))[0] == "witness":
                    self._seed_bond(db, npc_id, actor_id, witness.rank_to_actor, gt)
                    if target_id is not None:
                        self._seed_bond(db, npc_id, target_id, witness.rank_to_target, gt)
            event_id = db.execute(
                "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
                " game_key, real_time, duration_s, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, self._event_type(record), record.location,
                 json.dumps([actor_id] + ([target_id] if target_id else [])), json.dumps(self._payload(record, unseen)),
                 "game", gt, record.key, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                 record.duration_s, 1.0)).lastrowid
            outcome.event_id = event_id
            for npc_id, (role, seen) in people.items():
                db.execute("INSERT INTO event_witness (event_id, npc_id, how, distance_m, had_los) VALUES (?,?,?,?,?)",
                           (event_id, npc_id, seen.how, seen.distance_m,
                            None if seen.had_los is None else int(seen.had_los)))
                outcome.witnesses[npc_id] = seen.how

            for npc_id, (role, seen) in people.items():
                if self._is_player(db, npc_id):
                    continue   # the player keeps their own memories
                draft = self._draft(db, record, role, seen, npc_id, actor_id, target_id)
                if draft is None:
                    continue
                intoxication.hazy(draft, intoxication.drink_state(db, npc_id, gt).stage)
                about = [i for i in (actor_id, target_id) if i is not None and i != npc_id]
                mem_id = db.execute(
                    "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                    " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (timeline, npc_id, "episodic", draft.content, json.dumps(about), record.location, gt,
                     draft.importance, draft.valence, draft.intensity, "perception", draft.source_kind,
                     draft.interpretation, draft.confidence, event_id)).lastrowid
                db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
                outcome.memories[npc_id] = mem_id
                if draft.deltas_to_actor and npc_id != actor_id:
                    self._relationship_change(db, timeline, npc_id, actor_id, draft, mem_id, event_id, gt)
        logger.log(23, f"Living World: stored {record.type} event {event_id} ({record.key}); memories for "
                       f"{len(outcome.memories)} NPC(s); did not perceive it: {', '.join(unseen) or 'nobody'}")
        return outcome

    # ---- internals ----
    @staticmethod
    def _event_type(record: PerceptionRecord) -> str:
        return "physical_interaction" if record.type == "grab" else "consumption"

    @staticmethod
    def _payload(record: PerceptionRecord, unseen: list[str]) -> dict:
        if record.type == "grab":
            dur = record.duration_s or 0.0
            payload = {"interaction": "touch" if dur < formation.BRIEF_S else ("hold" if dur >= formation.LONG_S else "grab"),
                       "body_part": body_part(record.node), "node": record.node, "hand": record.hand,
                       "intent": "intentional", "detector": "higgs_grab"}
        else:
            payload = {"item": record.item, "item_form": record.item_form,
                       "category": item_category(record.item or "", record.is_food), "quantity": record.quantity,
                       "via": record.via, "confirmed": record.via in ("higgs_mouth", "equip", "action")}
        payload["nearby_but_unseen"] = unseen
        return payload

    @staticmethod
    def _is_player(db, npc_id: int) -> bool:
        return bool(db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0])

    @staticmethod
    def _bond(db, holder: int, other: int | None) -> Bond:
        if other is None or other == holder:
            return Bond()
        row = db.execute("SELECT status, kinship, trust FROM v_relationship WHERE from_id = ? AND to_id = ?",
                         (holder, other)).fetchone()
        return Bond(row[0], row[1], row[2]) if row else Bond()

    def _draft(self, db, record: PerceptionRecord, role: str, seen: Perception, npc_id: int, actor_id: int,
               target_id: int | None) -> MemoryDraft | None:
        if record.type == "grab":
            if role == "target":
                return formation.grab_for_target(record, self._bond(db, npc_id, actor_id))
            if role == "actor":
                return formation.grab_for_actor(record)
            return formation.grab_for_witness(record, seen, self._bond(db, npc_id, actor_id),
                                              self._bond(db, npc_id, target_id))
        if role == "actor":
            return formation.consume_for_actor(record)
        return formation.consume_for_witness(record, seen, self._bond(db, npc_id, actor_id))

    @staticmethod
    def _seed_bond(db, from_id: int, to_id: int, rank: int | None, gt: float) -> None:
        status = status_for_rank(rank)
        if status is None or from_id == to_id:
            return
        db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
                   (from_id, to_id, status, gt))

    @staticmethod
    def _relationship_change(db, timeline: int, from_id: int, to_id: int, draft: MemoryDraft, mem_id: int,
                             event_id: int, gt: float) -> None:
        # relationship holds the base values; event changes live only in relationship_change (timeline-scoped)
        db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
                   (from_id, to_id, "stranger", gt))
        db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_mem_id,"
                   " cause_event_id, reason, game_time) VALUES (?,?,?,?,?,?,?,?)",
                   (timeline, from_id, to_id, json.dumps(draft.deltas_to_actor), mem_id, event_id,
                    draft.interpretation or "perceived event", gt))
