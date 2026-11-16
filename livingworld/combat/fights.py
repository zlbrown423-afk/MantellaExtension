"""Group ``fight`` records into fights and store one event plus each NPC's own memory of it."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from livingworld.combat import WINDOW_DAYS
from livingworld.guard import identity_guard
from livingworld.squad import service as squad_service

logger = logging.getLogger("livingworld.combat")

EVENT_TYPE = "combat.fight"


def group(records: Iterable[Any]) -> list[list[Any]]:
    """Fight records split into fights: same place, each within ``WINDOW_DAYS`` of the one before."""
    fights: list[list[Any]] = []
    for r in sorted((r for r in records if r.type == "fight"), key=lambda r: r.game_time):
        place = (r.location or "").strip().lower()
        for fight in fights:
            last = fight[-1]
            if (last.location or "").strip().lower() == place and r.game_time - last.game_time <= WINDOW_DAYS:
                fight.append(r)
                break
        else:
            fights.append([r])
    return fights


def wound(hp: float | None) -> str | None:
    """How the NPC says it went for them, from the lowest share of health they fell to."""
    if hp is None:
        return None
    if hp >= 0.9:
        return "I came out of it without a scratch."
    if hp >= 0.6:
        return "I took a few knocks but stayed on my feet."
    if hp >= 0.3:
        return "I was hurt badly enough to feel it for days."
    return "I was nearly killed; it was close."


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def memory_for(own_hp: float | None, others: list[str], location: str | None, leader: str | None) -> dict[str, Any]:
    """One NPC's first-person memory of the fight. Nothing about enemies or the outcome: the game did not say."""
    where = f" near {location}" if location else ""
    patrol = f" while we were out on patrol with {leader}" if leader else ""
    text = f"I fought beside {_join(others)}{where}{patrol}." if others else f"I was in a fight{where}{patrol}."
    hurt = wound(own_hp)
    if hurt:
        text += " " + hurt
    hp = 1.0 if own_hp is None else own_hp
    importance = min(0.85, max(0.3, 0.45 + 0.05 * len(others) + (0.2 if hp < 0.3 else 0.1 if hp < 0.6 else 0.0)))
    return {"content": text, "importance": round(importance, 2),
            "valence": 0.1 if hp >= 0.6 else (-0.2 if hp >= 0.3 else -0.4),
            "intensity": round(min(1.0, 0.35 + 0.5 * (1.0 - hp)), 2), "confidence": 0.9}


def _label(db, npc_id: int) -> str:
    given = identity_guard.guard_name(db, npc_id)
    if given:
        return given
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else "someone"


def store_fights(pipeline: Any, records: Iterable[Any]) -> list[int]:
    """Store each fight in ``records`` that the player took part in beside at least one NPC. Returns event ids.
    ``pipeline`` is the ``PerceptionPipeline`` (identities, child check, store)."""
    out: list[int] = []
    for fight in group(records):
        event_id = _store_one(pipeline, fight)
        if event_id is not None:
            out.append(event_id)
    return out


def _store_one(pipeline: Any, fight: list[Any]) -> int | None:
    store = pipeline.store
    key = "fight:" + min(r.key for r in fight)
    timeline = store.active_timeline_id()
    # the game can send a fight again after an older save is loaded: look on every timeline this one can see
    if store.scalar("SELECT e.event_id FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id"
                    " WHERE e.game_key = ? AND e.game_time <= s.cutoff_gt", (key,)) is not None:
        return None
    gt = max(r.game_time for r in fight)
    location = next((r.location for r in fight if r.location), None)
    hp: dict[int, float | None] = {}
    for r in fight:
        npc_id = pipeline.identities.ensure_npc(r.actor.identity(), r.game_time)
        if pipeline._child(npc_id):
            continue   # children are left out of every Living World system
        prev = hp.get(npc_id)
        hp[npc_id] = r.hp if prev is None else (prev if r.hp is None else min(prev, r.hp))   # lowest it ever fell
    with store.transaction() as db:
        player_ids = [i for i in hp if pipeline._is_player(db, i)]
        npc_ids = [i for i in hp if i not in player_ids]
        if not player_ids or not npc_ids:
            return None   # only fights the player was in, with someone beside them, are shared experiences
        names = {i: _label(db, i) for i in hp}
        squads = {i: squad_service.squad_of(db, i) for i in npc_ids}
        event_id = db.execute(
            "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
            " game_key, real_time, duration_s, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (timeline, EVENT_TYPE, location, json.dumps(player_ids + npc_ids),
             json.dumps({"participants": [{"npc_id": i, "lowest_hp": hp[i]} for i in player_ids + npc_ids],
                         "record_keys": sorted(r.key for r in fight),
                         "squad_ids": sorted({s.squad_id for s in squads.values() if s is not None}),
                         "foes": "unknown", "outcome": "unknown"}),
             "game", gt, key, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), None, 1.0)).lastrowid
        for npc_id in player_ids + npc_ids:
            db.execute("INSERT INTO event_witness (event_id, npc_id, how, distance_m, had_los) VALUES (?,?,'participated',NULL,NULL)",
                       (event_id, npc_id))
        mem_of: dict[int, int] = {}
        for npc_id in npc_ids:
            others = [names[i] for i in player_ids + npc_ids if i != npc_id]
            squad = squads[npc_id]
            leader = _label(db, squad.leader_id) if squad is not None else None
            m = memory_for(hp[npc_id], others, location, leader)
            mem_id = db.execute(
                "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, npc_id, "episodic", m["content"], json.dumps([i for i in player_ids + npc_ids if i != npc_id]),
                 location, gt, m["importance"], m["valence"], m["intensity"], "perception", "experienced", None,
                 m["confidence"], event_id)).lastrowid
            db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
            mem_of[npc_id] = mem_id
        _bonds(db, timeline, event_id, gt, hp, mem_of, player_ids)
        _field_reports(db, timeline, event_id, npc_ids, location, gt)
    logger.log(23, f"Living World: stored fight event {event_id} near {location}: "
                   + ", ".join(f"{names[i]} (lowest health {hp[i]})" for i in player_ids + npc_ids))
    return event_id


def _field_reports(db, timeline: int, event_id: int, npc_ids: list[int], location: str | None, gt: float) -> None:
    """Set the fight against each member's assignment (livingworld/spatial). Never lets a failure undo the fight."""
    from livingworld.spatial import spatial_enabled
    if not spatial_enabled():
        return
    try:
        from livingworld.spatial import service as spatial
        db.execute("SAVEPOINT field_reports")
        spatial.record_fight(db, timeline, event_id, npc_ids, location, gt)
        db.execute("RELEASE field_reports")
    except Exception:
        db.execute("ROLLBACK TO field_reports")
        db.execute("RELEASE field_reports")
        logger.exception("Living World: could not record field reports; carrying on")


def _bonds(db, timeline: int, event_id: int, gt: float, hp: dict, mem_of: dict, player_ids: list[int]) -> None:
    """Fighting together draws people a little closer (combat/bonds). A failure never undoes the fight."""
    from livingworld.combat import bonds
    if not bonds.bonds_enabled():
        return
    try:
        db.execute("SAVEPOINT bonds")
        bonds.apply(db, timeline, event_id, gt, hp, mem_of, player_ids)
        db.execute("RELEASE bonds")
    except Exception:
        db.execute("ROLLBACK TO bonds")
        db.execute("RELEASE bonds")
        logger.exception("Living World: could not record fight bonds; carrying on")
