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
from livingworld.perception.records import (BadRecord, PerceptionRecord, grab_area, item_category, parse_record,
                                            status_for_rank)
from livingworld.store.identity import IdentityRegistry
from livingworld.store.minors import is_child
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
        if record.type in ("fight", "swim"):
            return self._hygiene(record)
        if record.type == "watch":
            return self._guard_watch(record)
        if record.type == "react":
            return self._reaction(record)
        timeline = self.store.active_timeline_id()
        # The game can send a record again after an older save is loaded (the save still has it queued).
        # Look on every timeline the active one can see, not only the active one, or the NPCs get the
        # same memory twice.
        existing = self.store.scalar(
            "SELECT e.event_id FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id"
            " WHERE e.game_key = ? AND e.game_time <= s.cutoff_gt", (record.key,))
        if existing is not None:
            return Outcome(existing, duplicate=True)

        gt = record.game_time
        actor_id = self.identities.ensure_npc(record.actor.identity(), gt)
        target_id = self.identities.ensure_npc(record.target.identity(), gt) if record.target else None
        party_id = self.identities.ensure_npc(record.party.identity(), gt) if record.party else None
        if record.type == "theft" and target_id is None and record.owner:
            target_id = self._owner_id(record.owner, actor_id)
        if any(self._child(i) for i in (actor_id, target_id, party_id)):
            # children are left out of every Living World system: no event, no memories, no claims
            logger.log(23, f"Living World: {record.type} record {record.key} involves a child; not stored")
            return Outcome(None)
        people: dict[int, tuple[str, Perception]] = {}   # npc_id -> (role, perception)
        people[actor_id] = ("actor", Perception("participated", "experienced", 1.0))
        if target_id is not None and record.type != "theft":
            people[target_id] = ("target", Perception("participated", "experienced", 1.0))
        # a theft victim only knows if they saw it (they are then a witness); a picked pocket goes unnoticed
        unseen = []
        witness_ids = []
        for witness in record.witnesses:
            npc_id = self.identities.ensure_npc(witness.actor.identity(), gt)
            if self._child(npc_id):
                continue   # children are never witnesses
            witness_ids.append((npc_id, witness))
            if npc_id in people:
                continue
            seen = formation.perceives(witness)
            if seen is not None and record.type == "theft" and record.via == "pickpocket" \
                    and (npc_id == target_id or (seen.distance_m or 0.0) > formation.CLEAR_VIEW_M):
                seen = None   # picking a pocket is quiet work: only someone close by and watching sees it
            if seen is None:
                unseen.append(witness.actor.name)
                continue
            people[npc_id] = ("witness", seen)

        outcome = Outcome(None, unseen=unseen)
        with self.store.transaction() as db:
            # the game's own relationship ranks seed relationship rows that do not exist yet
            if target_id is not None and target_id in people:
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

    def _hygiene(self, record: PerceptionRecord) -> Outcome:
        """Fights and swims only change how clean someone is: no event, no memories."""
        from livingworld.hygiene import model as hygiene
        actor_id = self.identities.ensure_npc(record.actor.identity(), record.game_time)
        if self._child(actor_id):
            return Outcome(None)
        with self.store.transaction() as db:
            if record.type == "fight":
                added = hygiene.record_fight(db, actor_id, record.game_time, record.hp, record.key)
                if added:
                    self._guard_fight(db, actor_id, record)
            else:
                added = hygiene.record_swim(db, actor_id, record.game_time, record.duration_s, record.key)
        return Outcome(None, duplicate=not added)

    @staticmethod
    def _guard_fight(db, actor_id: int, record: PerceptionRecord) -> None:
        """G2d: a fight where the guard sent the player (bandits) counts toward that duty."""
        from livingworld.guard import guard_enabled
        if not guard_enabled():
            return
        from livingworld.guard import duty as DU
        task = DU.mark_fought(db, actor_id, record.location, record.game_time)
        if task is not None:
            logger.log(23, f"Living World guard: fight at {record.location} counts for duty {task.task_id}")

    def _guard_watch(self, record: PerceptionRecord) -> Outcome:
        """G2e: a watch window on the player's guard shift came due (someone came over, or they noticed something)."""
        from livingworld.guard import guard_enabled
        if not guard_enabled():
            return Outcome(None)
        from livingworld.guard import encounter as ENC
        gt = record.game_time
        member_id = self.identities.ensure_npc(record.actor.identity(), gt)
        npc_id = self.identities.ensure_npc(record.target.identity(), gt) if record.target else None
        with self.store.transaction() as db:
            event_id = ENC.record_watch(db, member_id, npc_id, record.task or 0, record.win or 0,
                                        record.via or ENC.OBSERVE, gt, record.location)
        logger.log(23, f"Living World guard: watch window {record.win} of duty {record.task} ({record.via}) -> "
                       f"event {event_id}")
        return Outcome(event_id, duplicate=event_id is None)

    def _reaction(self, record: PerceptionRecord) -> Outcome:
        """Someone reacted on the spot to the player's hands on them (``react`` record). Kept as a world event so a
        guard knows whether colleagues have already had to step in (see ``guard/peace.py``)."""
        gt = record.game_time
        existing = self.store.scalar(
            "SELECT e.event_id FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id"
            " WHERE e.game_key = ? AND e.game_time <= s.cutoff_gt", (record.key,))
        if existing is not None:
            return Outcome(existing, duplicate=True)
        reactor = self.identities.ensure_npc(record.actor.identity(), gt)
        player = self.identities.ensure_npc(record.target.identity(), gt)
        victim = self.identities.ensure_npc(record.party.identity(), gt) if record.party else reactor
        if any(self._child(i) for i in (reactor, victim)):
            return Outcome(None)
        timeline = self.store.active_timeline_id()
        with self.store.transaction() as db:
            event_id = db.execute(
                "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
                " game_key, real_time, duration_s, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, "reaction", record.location, json.dumps([reactor, player, victim]),
                 json.dumps({"role": record.via, "area": record.area}), "game", gt, record.key,
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), record.duration_s, 1.0)).lastrowid
        logger.log(23, f"Living World: {record.actor.name} reacts ({record.via}) to {record.target.name}'s grab"
                       f" -> event {event_id}")
        return Outcome(event_id)

    # ---- internals ----
    def _child(self, npc_id: int | None) -> bool:
        if npc_id is None:
            return False
        with self.store.transaction() as db:
            return is_child(db, npc_id)

    @staticmethod
    def _event_type(record: PerceptionRecord) -> str:
        # a purchase is 'trade', never 'consumption': buying a round does not count as drinking it
        return {"grab": "physical_interaction", "purchase": "trade", "gift": "gift", "theft": "theft",
                "hit": "violence"}.get(record.type, "consumption")

    @staticmethod
    def _payload(record: PerceptionRecord, unseen: list[str]) -> dict:
        if record.type == "grab":
            dur = record.duration_s or 0.0
            payload = {"interaction": "touch" if dur < formation.BRIEF_S else ("hold" if dur >= formation.LONG_S else "grab"),
                       "body_part": grab_area(record), "node": record.node, "area": record.area, "hand": record.hand,
                       "intent": "intentional", "detector": "higgs_grab"}
        elif record.type == "purchase":
            payload = {"item": record.item, "item_form": record.item_form, "quantity": record.quantity,
                       "gold": record.gold, "payer": record.payer, "party": record.party.name if record.party else None,
                       "via": "action"}
        elif record.type in ("gift", "theft"):
            payload = {"item": record.item, "item_form": record.item_form, "quantity": record.quantity,
                       "gold": record.gold, "via": record.via}
            if record.type == "theft":
                payload["owner"] = record.owner
        elif record.type == "hit":
            payload = {"weapon": record.item, "via": record.via}
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
        if record.type in ("gift", "theft", "hit"):
            if role == "actor":
                return None   # only the player does these, and the player keeps their own memories
            if record.type == "gift":
                return formation.gift_for_target(record) if role == "target" else formation.gift_for_witness(record, seen)
            if record.type == "theft":
                return formation.theft_for_witness(record, seen, is_owner=npc_id == target_id,
                                                   to_owner=self._bond(db, npc_id, target_id))
            if role == "target":
                return formation.hit_for_target(record, self._bond(db, npc_id, actor_id))
            return formation.hit_for_witness(record, seen, self._bond(db, npc_id, actor_id),
                                             self._bond(db, npc_id, target_id))
        if record.type == "purchase":
            if role == "actor":
                return formation.purchase_for_actor(record)
            if role == "target":
                return formation.purchase_for_seller(record)
            return formation.purchase_for_witness(record, seen)
        if role == "actor":
            return formation.consume_for_actor(record)
        return formation.consume_for_witness(record, seen, self._bond(db, npc_id, actor_id))

    def _owner_id(self, name: str, actor_id: int) -> int | None:
        """The NPC the game named as a stolen item's owner, when we know them by that name."""
        return self.store.scalar("SELECT npc_id FROM npc WHERE display_name = ? AND npc_id <> ? AND is_player = 0"
                                 " ORDER BY npc_id LIMIT 1", (name, actor_id))

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
