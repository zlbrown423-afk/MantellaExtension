"""G2d: incidents, the trouble a hold's guard is sent to deal with: thefts, brawls, bandits on the road and
bandit camps.

An incident is a ``guard.incident`` world event (so it rolls back with saves and guards know of it); it
ends with a ``guard.incident_closed`` event. The member's superior hands an open incident out as a duty
(template ``duty:incident:<key>:<event_id>``) ahead of routine duties and whatever the hour. Thefts and
brawls are dealt with by being at the place (a conversation there, like a post); bandits only by
fighting there (the game's fight record carries the location). Reporting back closes the incident: the
culprit is arrested or the bandits driven off, written as ``guard.arrested`` / ``guard.cleared`` events
that gossip carries.

Culprits are people off-screen (no NPC is spawned or accused). When the shared court and records
arrive (civic C3/C4), an arrest becomes a case there instead of ending here.

Rules only: a seeded daily roll per incident type (``chance``), at most one open incident per type.
"""
from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from importlib import resources

from livingworld.guard import orgs as O, store as S

PREFIX = "duty:incident:"
OUTCOMES = ("arrested", "cleared")


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    why: str
    instructions: str
    where: tuple[str, ...]
    need: str               # 'seen' (be at the place) | 'fight' (fight there)
    outcome: str            # 'arrested' | 'cleared'
    place: str              # "in the Plains District market"
    shift_hours: float
    lasts_days: float
    chance: float
    min_rank: str | None
    merit: float
    complainants: tuple[str, ...]
    culprits: tuple[str, ...]
    camps: tuple[str, ...]
    suspects: tuple[str, ...] = ()     # G2f: real local people who may be named as the culprit
    suspect_chance: float = 0.0
    charge: str | None = None          # G2f: what an arrest for it is for ('theft', 'brawling')
    open_country: bool = False         # a fight anywhere in the unnamed wilderness counts (road bandits)


@dataclass
class Incident:
    event_id: int
    org_key: str
    key: str
    title: str
    why: str
    instructions: str
    where: tuple[str, ...]
    need: str
    outcome: str
    place: str
    shift_hours: float
    min_rank: str | None
    merit: float
    culprit: str | None
    opened_gt: float
    expires_gt: float
    day: int
    suspect: str | None = None         # G2f: a real person named as the culprit (arrest them for evidence)
    charge: str | None = None


@functools.lru_cache(maxsize=None)
def kinds(org_key: str) -> tuple[Kind, ...]:
    org = O.by_key(org_key)
    if org is None or not org.duty_file:
        return ()
    raw = json.loads(resources.files("livingworld.guard").joinpath("data", org.duty_file).read_text(encoding="utf-8"))
    return tuple(Kind(k["key"], k["title"], k.get("why", ""), k.get("instructions", ""), tuple(k.get("where") or ()),
                      k.get("need", "seen"), k.get("outcome", "arrested"), k.get("place", ""),
                      float(k.get("shift_hours", 4)), float(k.get("lasts_days", 1)), float(k.get("chance", 0.2)),
                      k.get("min_rank"), float(k.get("merit", 1.5)), tuple(k.get("complainants") or ()),
                      tuple(k.get("culprits") or ()), tuple(k.get("camps") or ()),
                      suspects=tuple(k.get("suspects") or ()), suspect_chance=float(k.get("suspect_chance", 0.0)),
                      charge=k.get("charge"), open_country=bool(k.get("open_country", False)))
                 for k in raw.get("incidents") or ())


def kind_of(inc: Incident) -> Kind | None:
    """The kind an incident was opened from (looked up live, so incidents already open get later data changes)."""
    return next((k for k in kinds(inc.org_key) if k.key == inc.key), None)


def _roll(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)


def _row(row) -> Incident:
    p = json.loads(row["payload_json"])
    return Incident(row["event_id"], p["org"], p["key"], p["title"], p.get("why", ""), p.get("instructions", ""),
                    tuple(p.get("where") or ()), p.get("need", "seen"), p.get("outcome", "arrested"), p.get("place", ""),
                    float(p.get("shift_hours", 4)), p.get("min_rank"), float(p.get("merit", 1.5)), p.get("culprit"),
                    row["game_time"], float(p["expires_gt"]), int(p.get("day", int(row["game_time"]))),
                    p.get("suspect"), p.get("charge"))


def get(db, event_id: int) -> Incident | None:
    row = db.execute("SELECT event_id, payload_json, game_time FROM v_visible_event WHERE event_id = ? AND type ="
                     " 'guard.incident'", (event_id,)).fetchone()
    return _row(row) if row else None


def _closed(db, event_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.incident_closed' AND"
                      " json_extract(payload_json, '$.incident') = ? LIMIT 1", (event_id,)).fetchone() is not None


def _taken(db, event_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_task WHERE template LIKE ? AND is_reset = 0 AND state IN"
                      " ('accepted','in_progress','paused','completed') LIMIT 1",
                      (f"{PREFIX}%:{event_id}",)).fetchone() is not None


def open_incidents(db, org: O.Org, now: float, *, free_only: bool = False) -> list[Incident]:
    rows = db.execute("SELECT event_id, payload_json, game_time FROM v_visible_event WHERE type = 'guard.incident'"
                      " AND json_extract(payload_json, '$.org') = ? AND game_time <= ? ORDER BY game_time, event_id",
                      (org.key, now + 1e-9)).fetchall()
    out = []
    for row in rows:
        inc = _row(row)
        if inc.expires_gt <= now or _closed(db, inc.event_id) or (free_only and _taken(db, inc.event_id)):
            continue
        out.append(inc)
    return out


def for_member(db, org: O.Org, svc: S.Service, now: float) -> list[Incident]:
    """Open incidents nobody has taken that the member's rank may be sent to."""
    mine = org.rank_index(svc.rank_key)
    return [i for i in open_incidents(db, org, now, free_only=True)
            if not i.min_rank or org.rank_index(i.min_rank) <= mine]


def spawn(db, org: O.Org, now: float, since: float | None = None) -> list[Incident]:
    """Roll each day not yet rolled (at most the last 3, none before the day after ``since``, when the member
    joined) and open the incidents that come up."""
    ks = kinds(org.key)
    if not ks:
        return []
    today = int(now)
    last = db.execute("SELECT MAX(json_extract(payload_json, '$.day')) FROM v_visible_event WHERE type ="
                      " 'guard.incident_roll' AND json_extract(payload_json, '$.org') = ?", (org.key,)).fetchone()[0]
    first = today if last is None else max(int(last) + 1, today - 2)
    if since is not None:
        first = max(first, int(since) + 1)
    made = []
    for day in range(first, today + 1):
        S.event(db, "incident_roll", [], {"org": org.key, "day": day}, min(now, day + 0.25), None, [])
        open_keys = {i.key for i in open_incidents(db, org, now)}
        for k in ks:
            if k.key in open_keys:
                continue
            seed = _roll(org.key, day, k.key)
            if (seed % 10_000) / 10_000 >= k.chance:
                continue
            opened = min(now, day + 0.25 + (seed >> 16) % 600 / 1440)       # some time from 6 am on
            if opened + k.lasts_days <= now:
                continue                                                  # came and went while nobody looked
            made.append(_open(db, org, k, day, opened, seed))
    return made


def _open(db, org: O.Org, k: Kind, day: int, opened: float, seed: int) -> Incident:
    pick = lambda xs, shift: xs[(seed >> shift) % len(xs)] if xs else ""   # noqa: E731
    fill = {"complainant": pick(k.complainants, 24), "camp": pick(k.camps, 32)}
    title, why, place = (s.format(**fill) for s in (k.title, k.why, k.place))
    where = tuple(w.format(**fill) for w in k.where)
    culprit = pick(k.culprits, 40) or None
    suspect = None
    if k.suspects and ((seed >> 48) % 1000) / 1000 < k.suspect_chance:
        suspect = culprit = pick(k.suspects, 56)
        why += f". Word is it was {suspect}"
    payload = {"org": org.key, "key": k.key, "title": title, "why": why, "instructions": k.instructions.format(**fill),
               "where": list(where), "need": k.need, "outcome": k.outcome, "place": place,
               "shift_hours": k.shift_hours, "min_rank": k.min_rank, "merit": k.merit, "culprit": culprit,
               "expires_gt": opened + k.lasts_days, "day": day, "suspect": suspect, "charge": k.charge}
    event_id = S.event(db, "incident", [], payload, opened, where[0] if where else org.hold, [])
    return get(db, event_id)


def close(db, inc: Incident, outcome: str, gt: float, *, member_id: int | None = None, by: int | None = None,
          witnesses: list[int] | None = None, source: str = "sim") -> None:
    """End an incident; a member's success becomes news (``guard.arrested`` / ``guard.cleared``)."""
    S.event(db, "incident_closed", [a for a in (member_id, by) if a is not None],
            {"org": inc.org_key, "incident": inc.event_id, "outcome": outcome}, gt, None, [], source)
    if member_id is not None and outcome in OUTCOMES:
        S.event(db, outcome, [member_id, by], {"org": inc.org_key, "incident": inc.event_id, "culprit": inc.culprit,
                                               "place": inc.place, "title": inc.title}, gt,
                inc.where[0] if inc.where else None, witnesses, source)


def expire(db, org: O.Org, now: float) -> list[int]:
    """Close incidents whose time ran out with nobody dealing with them (they go cold, no news)."""
    out = []
    rows = db.execute("SELECT event_id, payload_json, game_time FROM v_visible_event WHERE type = 'guard.incident'"
                      " AND json_extract(payload_json, '$.org') = ?", (org.key,)).fetchall()
    for row in rows:
        inc = _row(row)
        if inc.expires_gt <= now and not _closed(db, inc.event_id) and not _held(db, inc.event_id):
            close(db, inc, "went cold", inc.expires_gt)
            out.append(inc.event_id)
    return out


def _held(db, event_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_task WHERE template LIKE ? AND is_reset = 0 AND state IN"
                      " ('accepted','in_progress','paused') LIMIT 1", (f"{PREFIX}%:{event_id}",)).fetchone() is not None


def event_id_of(template: str | None) -> int | None:
    if not template or not template.startswith(PREFIX):
        return None
    try:
        return int(template.rsplit(":", 1)[1])
    except ValueError:
        return None
