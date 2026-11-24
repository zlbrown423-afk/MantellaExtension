"""Rows for territory: the ownership mirror, the garrison roster and what people know. Callers own the transaction."""
from __future__ import annotations

import json
from dataclasses import dataclass

from livingworld.store.minors import is_child
from livingworld.territory import EVENT_PREFIX
from livingworld.territory.sites import Site, by_key


def timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


@dataclass(frozen=True)
class State:
    site_key: str
    owner: str
    strength: int
    contested: bool
    held_since: float | None
    cooldown_gt: float | None
    changed_gt: float | None
    previous_owner: str | None = None


def state_of(db, site_key: str) -> State:
    row = db.execute("SELECT * FROM v_territory_site WHERE site_key = ?", (site_key,)).fetchone()
    if row is None:
        return State(site_key, "unoccupied", 0, False, None, None, None)
    return State(site_key, row["owner_after"], row["strength"], bool(row["contested"]), row["held_since"],
                 row["cooldown_gt"], row["game_time"], row["owner_before"])


def states(db) -> dict[str, State]:
    return {r["site_key"]: State(r["site_key"], r["owner_after"], r["strength"], bool(r["contested"]), r["held_since"],
                                 r["cooldown_gt"], r["game_time"], r["owner_before"])
            for r in db.execute("SELECT * FROM v_territory_site")}


def changes(db, site_key: str, limit: int = 20) -> list:
    return db.execute("SELECT * FROM v_territory_change WHERE site_key = ? ORDER BY game_time DESC, change_id DESC LIMIT ?",
                      (site_key, limit)).fetchall()


def event(db, kind: str, site: Site, payload: dict, gt: float, witnesses: list[int], source: str = "game",
          game_key: str | None = None) -> int:
    """A ``territory.<kind>`` world event. ``actors`` stay empty so the gossip appraisal never makes a claim about
    a person from it; people learn it through ``territory_knowledge`` instead."""
    body = {"location_key": site.key, "location_name": site.name, "form_id": site.form_id, "hold": site.hold,
            "source": payload.get("source", "game"), "confidence": payload.get("confidence", 1.0), **payload}
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time, game_key)"
        " VALUES (?,?,?, '[]', ?, ?, ?, ?)",
        (timeline(db), EVENT_PREFIX + kind, site.location, json.dumps(body, sort_keys=True),
         "game" if source == "game" else "admin", gt, game_key)).lastrowid
    for npc_id in dict.fromkeys(witnesses):
        if npc_id is not None and not is_child(db, npc_id):
            db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'saw')", (event_id, npc_id))
    return event_id


def add_change(db, site_key: str, kind: str, before: str, after: str, strength: int, contested: bool, held_since: float,
               cooldown: float | None, gt: float, event_id: int | None, game_key: str | None, source: str = "game") -> int:
    return db.execute(
        "INSERT INTO territory_change (timeline_id, site_key, kind, owner_before, owner_after, strength, contested,"
        " held_since, cooldown_gt, source, event_id, game_key, game_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (timeline(db), site_key, kind, before, after, strength, int(contested), held_since, cooldown, source, event_id,
         game_key, gt)).lastrowid


# ---- garrison ----

@dataclass(frozen=True)
class Post:
    assign_id: int
    site_key: str
    npc_id: int
    faction: str
    rank: str
    squad: str | None
    duty: str | None
    equipment: tuple[str, ...]
    assigned_gt: float
    health: float | None


def _post(r) -> Post:
    return Post(r["assign_id"], r["site_key"], r["npc_id"], r["faction"], r["rank"], r["squad"], r["duty"],
                tuple(json.loads(r["equipment_json"] or "[]")), r["assigned_gt"], r["health"])


def post_of(db, npc_id: int) -> Post | None:
    r = db.execute("SELECT * FROM v_garrison WHERE npc_id = ? ORDER BY assigned_gt DESC, assign_id DESC LIMIT 1",
                   (npc_id,)).fetchone()
    return _post(r) if r else None


def roster(db, site_key: str, faction: str | None = None) -> list[Post]:
    sql = "SELECT * FROM v_garrison WHERE site_key = ?" + (" AND faction = ?" if faction else "") + " ORDER BY assign_id"
    return [_post(r) for r in db.execute(sql, (site_key, faction) if faction else (site_key,))]


def commander(db, site_key: str, faction: str = "wardens") -> Post | None:
    return next((p for p in roster(db, site_key, faction) if p.rank == "commander"), None)


def assign(db, site_key: str, npc_id: int, faction: str, gt: float, *, rank: str = "soldier", squad: str | None = None,
           duty: str | None = None, equipment: list[str] | None = None, health: float | None = None,
           game_key: str | None = None) -> int | None:
    """Post someone to a site. A person with a live post elsewhere is moved (the old one ends as 'transferred'); the
    same post again changes nothing. Returns the assignment id, or None for a child or a repeated record."""
    if is_child(db, npc_id):
        return None
    if game_key and db.execute("SELECT 1 FROM garrison_assignment WHERE timeline_id = ? AND game_key = ?",
                               (timeline(db), game_key)).fetchone():
        return None
    now = post_of(db, npc_id)
    if now is not None:
        if now.site_key == site_key and now.faction == faction and now.rank == rank and now.squad == squad \
                and now.duty == duty:
            return None
        end(db, npc_id, "transferred", gt, note=f"to {site_key}")
    assign_id = db.execute(
        "INSERT INTO garrison_assignment (timeline_id, site_key, npc_id, faction, rank, squad, duty, equipment_json,"
        " assigned_gt, game_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (timeline(db), site_key, npc_id, faction, rank, squad, duty, json.dumps(equipment or []), gt, game_key)).lastrowid
    db.execute("INSERT INTO garrison_status (timeline_id, assign_id, status, health, note, game_time)"
               " VALUES (?,?, 'assigned', ?, '', ?)", (timeline(db), assign_id, health, gt))
    if rank == "commander":   # one commander a post: the previous one is a plain soldier again
        for other in roster(db, site_key, faction):
            if other.npc_id != npc_id and other.rank == "commander":
                db.execute("INSERT INTO garrison_status (timeline_id, assign_id, status, note, game_time)"
                           " VALUES (?,?, 'replaced', 'a new commander', ?)", (timeline(db), other.assign_id, gt))
                assign(db, site_key, other.npc_id, other.faction, gt + 1e-6, rank="soldier", squad=other.squad,
                       duty=other.duty, equipment=list(other.equipment), health=other.health)
    return assign_id


def end(db, npc_id: int, status: str, gt: float, note: str = "", health: float | None = None) -> bool:
    now = post_of(db, npc_id)
    if now is None:
        return False
    db.execute("INSERT INTO garrison_status (timeline_id, assign_id, status, health, note, game_time) VALUES (?,?,?,?,?,?)",
               (timeline(db), now.assign_id, status, health, note, gt))
    return True


# ---- knowledge ----

def learn(db, holder_id: int, site_key: str, owner: str, strength: int, contested: bool, origin: str, confidence: float,
          about_gt: float, learned_gt: float, change_id: int | None, told_by: int | None = None) -> bool:
    if is_child(db, holder_id) or (told_by is not None and (told_by == holder_id or is_child(db, told_by))):
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO territory_knowledge (timeline_id, holder_id, site_key, owner, strength, contested,"
        " origin, told_by_id, confidence, about_gt, learned_gt, change_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (timeline(db), holder_id, site_key, owner, strength, int(contested), origin, told_by,
         max(0.0, min(1.0, confidence)), about_gt, learned_gt, change_id))
    return cur.rowcount > 0


def known(db, holder_id: int) -> list:
    """The freshest thing this person knows about each site (the later fact wins, a firmer source wins a tie)."""
    rows = db.execute("SELECT * FROM v_territory_knowledge WHERE holder_id = ? ORDER BY site_key, about_gt DESC,"
                      " confidence DESC, know_id DESC", (holder_id,)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["site_key"] not in seen:
            seen.add(r["site_key"])
            out.append(r)
    return out


def name_of(site_key: str) -> str:
    s = by_key(site_key)
    return s.name if s else site_key
