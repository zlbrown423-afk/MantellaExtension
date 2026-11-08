"""The service record store: enlistments, rank and status changes, and the guard events gossip reads.

Every change is a ``service_change`` row plus (for anything people would notice) a ``guard.<kind>``
world event with witnesses, both on the active save timeline; ``v_service`` gives the current view.
All functions take an open connection inside a transaction (``WorldStore.transaction()``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from livingworld.store.minors import is_child

SERVING = ("probation", "active", "suspended")     # still a member
ENDED = ("discharged", "dismissed", "resigned")
STATUSES = SERVING + ENDED
PATHS = ("applied", "invited", "thane", "sponsor", "earned", "admin")


@dataclass
class Service:
    service_id: int
    org_key: str
    member_id: int
    path: str
    recruiter_id: int | None
    sponsor_id: int | None
    location: str | None
    joined_gt: float
    rank_key: str | None
    rank_gt: float | None
    status: str | None
    status_gt: float | None
    merit: float

    @property
    def serving(self) -> bool:
        return self.status in SERVING


def _timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def _row(row) -> Service:
    keys = row.keys()
    return Service(**{f: row[f] for f in Service.__dataclass_fields__ if f in keys})


def get(db, service_id: int) -> Service | None:
    row = db.execute("SELECT * FROM v_service WHERE service_id = ?", (service_id,)).fetchone()
    return _row(row) if row else None


def find(db, where: str = "1", params: tuple = (), include_reset: bool = False) -> list[Service]:
    keep = "" if include_reset else " AND is_reset = 0"
    return [_row(r) for r in db.execute(f"SELECT * FROM v_service WHERE ({where}){keep} ORDER BY joined_gt, service_id",
                                        params)]


def current(db, member_id: int, org_key: str | None = None) -> Service | None:
    """The member's service that is still running (in ``org_key`` if given), or None."""
    marks = ",".join("?" * len(SERVING))
    where = f"member_id = ? AND status IN ({marks})" + (" AND org_key = ?" if org_key else "")
    params = (member_id, *SERVING) + ((org_key,) if org_key else ())
    rows = find(db, where, params)
    return rows[-1] if rows else None


def history(db, member_id: int, org_key: str | None = None) -> list[Service]:
    if org_key:
        return find(db, "member_id = ? AND org_key = ?", (member_id, org_key))
    return find(db, "member_id = ?", (member_id,))


def changes(db, service_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT change_id, rank_key, status, d_merit, reason, game_time FROM v_service_change WHERE service_id = ?"
        " ORDER BY game_time, change_id", (service_id,))]


def event(db, kind: str, actors: list[int | None], payload: dict, gt: float, location: str | None = None,
          witnesses: list[int] | None = None, source: str = "sim") -> int:
    """A ``guard.<kind>`` world event. actors[0] is the member, actors[1] the leader who acted, if any."""
    actors = [a for a in actors if a is not None]
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?,?,?,?,?,?,?)",
        (_timeline(db), f"guard.{kind}", location, json.dumps(actors), json.dumps(payload), source, gt)).lastrowid
    for npc_id in dict.fromkeys(list(actors) + list(witnesses or [])):
        if npc_id is not None and not is_child(db, npc_id):
            db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
                       (event_id, npc_id, "participated" if npc_id in actors else "saw"))
    return event_id


def enlist(db, org_key: str, member_id: int, path: str, gt: float, *, rank_key: str, status: str = "probation",
           recruiter_id: int | None = None, sponsor_id: int | None = None, location: str | None = None,
           witnesses: list[int] | None = None, source: str = "sim", reason: str = "taken on") -> Service:
    if is_child(db, member_id):
        raise ValueError("children never serve")
    if path not in PATHS:
        raise ValueError(f"unknown path {path!r}")
    service_id = db.execute(
        "INSERT INTO service (timeline_id, org_key, member_id, path, recruiter_id, sponsor_id, location, joined_gt)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (_timeline(db), org_key, member_id, path, recruiter_id, sponsor_id, location, gt)).lastrowid
    event_id = event(db, "enlisted", [member_id, recruiter_id],
                     {"service_id": service_id, "org": org_key, "path": path, "rank": rank_key,
                      "sponsor_id": sponsor_id}, gt, location, witnesses, source)
    db.execute("INSERT INTO service_change (timeline_id, service_id, rank_key, status, reason, cause_event_id,"
               " game_time) VALUES (?,?,?,?,?,?,?)",
               (_timeline(db), service_id, rank_key, status, reason, event_id, gt))
    return get(db, service_id)


def change(db, svc: Service, reason: str, gt: float, *, rank_key: str | None = None, status: str | None = None,
           d_merit: float = 0.0, kind: str | None = None, actor_id: int | None = None, payload: dict | None = None,
           location: str | None = None, witnesses: list[int] | None = None, source: str = "sim") -> Service:
    """Record a change; ``kind`` (e.g. 'promoted', 'dismissed') also writes a ``guard.<kind>`` event."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if rank_key == svc.rank_key:
        rank_key = None
    if status == svc.status:
        status = None
    event_id = None
    if kind:
        event_id = event(db, kind, [svc.member_id, actor_id],
                         {"service_id": svc.service_id, "org": svc.org_key, "reason": reason,
                          "rank_from": svc.rank_key, "rank": rank_key or svc.rank_key, "status": status or svc.status,
                          **(payload or {})}, gt, location or svc.location, witnesses, source)
    db.execute("INSERT INTO service_change (timeline_id, service_id, rank_key, status, d_merit, reason, cause_event_id,"
               " game_time) VALUES (?,?,?,?,?,?,?,?)",
               (_timeline(db), svc.service_id, rank_key, status, d_merit, reason, event_id, gt))
    return get(db, svc.service_id)


def last_event_gt(db, kind: str, member_id: int, org_key: str | None = None, actor_id: int | None = None) -> float | None:
    """When the last ``guard.<kind>`` event about this member happened (optionally in one org / by one leader)."""
    sql = ("SELECT MAX(game_time) FROM v_visible_event WHERE type = ? AND json_extract(actors_json, '$[0]') = ?")
    params: list = [f"guard.{kind}", member_id]
    if org_key:
        sql += " AND json_extract(payload_json, '$.org') = ?"
        params.append(org_key)
    if actor_id is not None:
        sql += " AND json_extract(actors_json, '$[1]') = ?"
        params.append(actor_id)
    return db.execute(sql, tuple(params)).fetchone()[0]
