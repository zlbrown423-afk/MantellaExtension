"""The charter itself: granting it, its current status, and the facts the reckoning judges it by.

Facts the game (L2 bridge) or the test commands report are ``lordship.*`` world events on the save timeline:
``lordship.strength`` {riders, horses}, ``lordship.patrol`` {route, count}, ``lordship.report`` {}. A term is only
judged once the game has declared it counts that kind of fact (``lordship.watching``, from the L2 bridge) or
such a fact has been reported under this charter; until then it is excused ("not yet counted in game"), so a
player is never punished for something the game cannot see yet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from livingworld.civic import institutions, ledger
from livingworld.lordship import COMPANY, COURT
from livingworld.lordship import data as D
from livingworld.store.minors import is_child

LIVE = ("active", "under_review", "suspended")
FACT_KINDS = ("strength", "patrol", "report")


@dataclass(frozen=True)
class Charter:
    charter_id: int
    grantor: str
    holder: str
    holder_npc_id: int
    version: int
    parent_charter_id: int | None
    level: int
    district_key: str | None
    terms: dict[str, Any]
    signed_gt: float
    review_gt: float | None
    status: str | None
    status_gt: float | None

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def title(self) -> str:
        return str(self.terms.get("title") or f"level {self.level}")


def _row(r) -> Charter:
    return Charter(r["charter_id"], r["grantor"], r["holder"], r["holder_npc_id"], r["version"],
                   r["parent_charter_id"], r["level"], r["district_key"], json.loads(r["terms_json"]),
                   r["signed_gt"], r["review_gt"], r["status"], r["status_gt"])


def current(db, holder: str = COMPANY) -> Charter | None:
    """The newest charter of this holder that the current save can see and that is still in force."""
    marks = ",".join("?" * len(LIVE))
    r = db.execute(f"SELECT * FROM v_charter WHERE holder = ? AND status IN ({marks})"
                   " ORDER BY signed_gt DESC, charter_id DESC LIMIT 1", (holder, *LIVE)).fetchone()
    return _row(r) if r else None


def latest(db, holder: str = COMPANY) -> Charter | None:
    """The newest charter whatever its status (revoked and superseded ones too)."""
    r = db.execute("SELECT * FROM v_charter WHERE holder = ? ORDER BY signed_gt DESC, charter_id DESC LIMIT 1",
                   (holder,)).fetchone()
    return _row(r) if r else None


def event(db, etype: str, gt: float, actors: list[int], payload: dict, location: str | None = None,
          source: str = "sim", witnesses: list[int] | tuple = ()) -> int:
    cur = db.execute("INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source,"
                     " game_time) VALUES (?,?,?,?,?,?,?)",
                     (institutions.active_timeline(db), etype, location, json.dumps(actors), json.dumps(payload),
                      source, gt))
    eid = cur.lastrowid
    for w in witnesses:
        if w not in actors and not is_child(db, w):
            db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,'saw')", (eid, w))
    for a in actors:
        if not is_child(db, a):
            db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,'participated')",
                       (eid, a))
    return eid


def set_status(db, charter_id: int, status: str, reason: str, gt: float, event_id: int | None = None) -> None:
    db.execute("INSERT INTO charter_change (timeline_id, charter_id, status, reason, cause_event_id, game_time)"
               " VALUES (?,?,?,?,?,?)", (institutions.active_timeline(db), charter_id, status, reason, event_id, gt))


def npc_for(db, official: D.Official) -> int | None:
    """The official's npc_id in this world, once the player has met them (None before)."""
    want = D.norm_form(official.base_id)
    for npc_id, plugin, base in db.execute("SELECT npc_id, plugin, base_form_id FROM npc WHERE is_player = 0"
                                           " ORDER BY npc_id").fetchall():
        if D.norm_form(base) == want and (plugin or "").lower() == official.plugin.lower():
            return npc_id
    return None


def official_of(db, npc_id: int) -> D.Official | None:
    r = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return D.official_for(r[0], r[1]) if r else None


def grant(db, player_id: int, gt: float, level: int = 1, district_key: str = "western_plains",
          conv_id: int | None = None, source: str = "game", reason: str = "the Jarl granted the charter",
          overrides: dict[str, Any] | None = None) -> Charter:
    """Sign a charter for the Plains Wardens. An existing live charter is superseded by this new version."""
    if D.district(district_key) is None:
        raise ValueError(f"no district {district_key!r}")
    terms = D.level_terms(level)
    terms.update(overrides or {})
    jarl = D.official("balgruuf")
    jarl_id = npc_for(db, jarl) if jarl else None
    institutions.ensure(db, COURT, "the Jarl's court of Whiterun", "court", gt, "Whiterun", jarl_id)
    institutions.ensure(db, COMPANY, "the Plains Wardens", "company", gt, "Whiterun", player_id)
    old = current(db)
    version = (old.version + 1) if old else 1
    review = gt + float(terms.get("review_after_days") or 90)
    cur = db.execute("INSERT INTO charter (timeline_id, grantor, holder, holder_npc_id, version, parent_charter_id,"
                     " level, district_key, terms_json, signed_gt, review_gt, conv_id, source)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (institutions.active_timeline(db), COURT, COMPANY, player_id, version,
                      old.charter_id if old else None, level, district_key, json.dumps(terms), gt, review,
                      conv_id, source))
    charter_id = cur.lastrowid
    actors = [player_id] + ([jarl_id] if jarl_id else [])
    eid = event(db, "lordship.charter_granted", gt, actors,
                {"charter_id": charter_id, "level": level, "title": terms.get("title"), "district": district_key,
                 "version": version}, location="Dragonsreach", source="admin" if source == "admin" else "game")
    if old is not None:
        set_status(db, old.charter_id, "superseded", f"replaced by version {version}", gt, eid)
    set_status(db, charter_id, "active", reason, gt, eid)
    return current(db)


def report_fact(db, kind: str, gt: float, player_id: int | None, payload: dict | None = None,
                source: str = "game") -> int:
    """Record a fact the reckoning judges: strength {riders, horses}, patrol {route, count}, report {}."""
    if kind not in FACT_KINDS:
        raise ValueError(f"unknown lordship fact {kind!r}")
    actors = [player_id] if player_id else []
    return event(db, f"lordship.{kind}", gt, actors, payload or {}, source=source)


def facts(db, kind: str, since_gt: float, until_gt: float) -> list[dict]:
    rows = db.execute("SELECT payload_json, game_time FROM v_visible_event WHERE type = ? AND game_time > ?"
                      " AND game_time <= ? ORDER BY game_time, event_id", (f"lordship.{kind}", since_gt, until_gt))
    return [dict(json.loads(p), game_time=g) for p, g in rows.fetchall()]


def declare_watching(db, kinds: list[str] | tuple[str, ...], gt: float, source: str = "game") -> int:
    """The game says it now counts these kinds of fact (the L2 bridge at load; tests and debug by hand). From then
    on a missing fact is a missed term, not an excused one."""
    bad = [k for k in kinds if k not in FACT_KINDS]
    if bad:
        raise ValueError(f"unknown lordship fact kinds {bad}")
    return event(db, "lordship.watching", gt, [], {"kinds": sorted(set(kinds))}, source=source)


def watched(db, kind: str, charter: Charter, until_gt: float) -> bool:
    """True when the term can be judged: the game has declared it counts this kind of fact, or such a fact has
    been reported under this charter."""
    if db.execute("SELECT 1 FROM v_visible_event WHERE type = ? AND game_time >= ? AND game_time <= ? LIMIT 1",
                  (f"lordship.{kind}", charter.signed_gt, until_gt)).fetchone() is not None:
        return True
    for (payload,) in db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'lordship.watching'"
                                 " AND game_time <= ?", (until_gt,)).fetchall():
        if kind in json.loads(payload).get("kinds", []):
            return True
    return False


def strength(db, at_gt: float) -> tuple[int, int]:
    """Riders and horses as last reported at or before ``at_gt`` (the slice's defaults before any report)."""
    t = D.terms()
    r = db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'lordship.strength' AND game_time <= ?"
                   " ORDER BY game_time DESC, event_id DESC LIMIT 1", (at_gt,)).fetchone()
    if r is None:
        return t.default_riders, t.default_horses
    p = json.loads(r[0])
    return int(p.get("riders", t.default_riders)), int(p.get("horses", t.default_horses))


def treasury(db, at_gt: float | None = None) -> int:
    return ledger.balance(db, ledger.purse_of(COMPANY), at_gt)
