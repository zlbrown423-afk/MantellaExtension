"""What the intimacy records share: storing one event with its witnesses and memories, and whose partner a
witness is.

Every record here is stored the same way as ``perception/``: one ``world_event``, one ``event_witness`` row per
person who took part or perceived it, one first-person ``memory`` per NPC (the player keeps their own), and
relationship changes only where a memory carries them. Children are never stored as anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from livingworld.perception import formation, intoxication
from livingworld.perception.formation import MemoryDraft, Perception
from livingworld.perception.records import PerceptionRecord


def existing_event(store, key: str):
    return store.scalar(
        "SELECT e.event_id FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id"
        " WHERE e.game_key = ? AND e.game_time <= s.cutoff_gt", (key,))


def seers(pipeline, record: PerceptionRecord, skip: set[int]) -> tuple[dict[int, Perception], list[str]]:
    """Witnesses who perceived it (npc_id -> how well), and the names of those nearby who did not."""
    seen: dict[int, Perception] = {}
    unseen: list[str] = []
    for witness in record.witnesses:
        npc_id = pipeline.identities.ensure_npc(witness.actor.identity(), record.game_time)
        if npc_id in skip or pipeline._child(npc_id):
            continue
        how = formation.perceives(witness)
        if how is None:
            unseen.append(witness.actor.name)
        elif npc_id not in seen or how.confidence > seen[npc_id].confidence:
            seen[npc_id] = how
    return seen, unseen


def store(pipeline, record: PerceptionRecord, etype: str, actor_ids: list[int], payload: dict,
          participants: dict[int, str], seen: dict[int, Perception],
          drafts: dict[int, tuple[MemoryDraft, list[int]]]):
    """Writes the event, its witnesses and the drafted memories. ``participants``: npc_id -> event_witness.how
    for the people it happened to; ``drafts``: npc_id -> (memory, who its relationship deltas are towards)."""
    from livingworld.perception.pipeline import Outcome
    gt = record.game_time
    timeline = pipeline.store.active_timeline_id()
    outcome = Outcome(None, unseen=list(payload.get("nearby_but_unseen") or []))
    with pipeline.store.transaction() as db:
        event_id = db.execute(
            "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
            " game_key, real_time, duration_s, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (timeline, etype, record.location, json.dumps(actor_ids), json.dumps(payload), "game", gt, record.key,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), record.duration_s, 1.0)).lastrowid
        outcome.event_id = event_id
        for npc_id, how in participants.items():
            db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)", (event_id, npc_id, how))
            outcome.witnesses[npc_id] = how
        for npc_id, how in seen.items():
            if npc_id in participants:
                continue
            db.execute("INSERT INTO event_witness (event_id, npc_id, how, distance_m, had_los) VALUES (?,?,?,?,?)",
                       (event_id, npc_id, how.how, how.distance_m, 1))
            outcome.witnesses[npc_id] = how.how
        for npc_id, (draft, towards) in drafts.items():
            if pipeline._is_player(db, npc_id):
                continue                     # the player keeps their own memories
            intoxication.hazy(draft, intoxication.drink_state(db, npc_id, gt).stage)
            about = [i for i in actor_ids if i != npc_id]
            mem_id = db.execute(
                "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, npc_id, "episodic", draft.content, json.dumps(about), record.location, gt,
                 draft.importance, draft.valence, draft.intensity, "perception", draft.source_kind,
                 draft.interpretation, draft.confidence, event_id)).lastrowid
            db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
            outcome.memories[npc_id] = mem_id
            if draft.deltas_to_actor:
                for other in towards:
                    if other != npc_id:
                        pipeline._relationship_change(db, timeline, npc_id, other, draft, mem_id, event_id, gt)
    return outcome


def partner_in(db, witness: int, people: list[int]) -> int | None:
    """The person in ``people`` the witness is with (spouse, or courting, dating, partners, betrothed), if any."""
    from livingworld.social.romance import ACTIVE, bond
    for person in people:
        if person == witness:
            continue
        b = bond(db, witness, person)
        if b is not None and b["stage"] in ACTIVE and b["stage"] != "affair":
            return person
        row = db.execute("SELECT kinship, status FROM v_relationship WHERE from_id = ? AND to_id = ?",
                         (witness, person)).fetchone()
        if row and (row[0] == "spouse" or row[1] in ("spouse", "partner", "courting")):
            return person
    return None


def partners_of(db, npc_id: int, scenes) -> set[int]:
    """Of the people in the running scenes, the ones ``npc_id`` is with."""
    out = set()
    for scene in scenes:
        people = [i for i in scene.people if i != npc_id]
        while people:
            found = partner_in(db, npc_id, people)
            if found is None:
                break
            out.add(found)
            people.remove(found)
    return out


def place(record: PerceptionRecord) -> str:
    return f" in {record.location}" if record.location else ""


def hedge(seen: Perception) -> str:
    return "I saw" if seen.confidence >= 0.8 else "I think I saw"
