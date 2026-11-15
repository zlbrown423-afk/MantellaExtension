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
    losses: tuple[str, ...] = ()       # what the complainant lost ({loss})
    complaint: str = ""                # the complainant's account of it (for the prompt)
    memory: str = ""                   # the same in their own words (their memory of it)
    culprit_looks: tuple[str, ...] = ()  # the glimpse they caught of each culprit (same order as culprits)


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
    complainant: str | None = None     # the townsperson who reported it (a real NPC; they remember doing so)
    complaint: str = ""                # their account of it
    look: str | None = None            # what they saw of the culprit (never the name)
    memory: str = ""                   # their own words for it


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
                      charge=k.get("charge"), open_country=bool(k.get("open_country", False)),
                      losses=tuple(k.get("losses") or ()), complaint=k.get("complaint", ""), memory=k.get("memory", ""),
                      culprit_looks=tuple(k.get("culprit_looks") or ()))
                 for k in raw.get("incidents") or ())


def kind_of(inc: Incident) -> Kind | None:
    """The kind an incident was opened from (looked up live, so incidents already open get later data changes)."""
    return next((k for k in kinds(inc.org_key) if k.key == inc.key), None)


def _roll(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)


def _row(row) -> Incident:
    p = json.loads(row["payload_json"])
    inc = Incident(row["event_id"], p["org"], p["key"], p["title"], p.get("why", ""), p.get("instructions", ""),
                   tuple(p.get("where") or ()), p.get("need", "seen"), p.get("outcome", "arrested"), p.get("place", ""),
                   float(p.get("shift_hours", 4)), p.get("min_rank"), float(p.get("merit", 1.5)), p.get("culprit"),
                   row["game_time"], float(p["expires_gt"]), int(p.get("day", int(row["game_time"]))),
                   p.get("suspect"), p.get("charge"), p.get("complainant"), p.get("complaint") or "", p.get("look"),
                   p.get("memory") or "")
    if inc.complainant is None:
        # opened before complainants were stored: the name is only in the 'why' ("Fralia Gray-Mane had a purse ...")
        k = kind_of(inc)
        inc.complainant = next((c for c in (k.complainants if k else ()) if inc.why.startswith(c)), None)
        if inc.complainant and k and k.complaint:
            inc.complaint = k.complaint.format(complainant=inc.complainant, loss="a purse", camp="")
            inc.memory = k.memory.format(complainant=inc.complainant, loss="my purse", camp="")
    return inc


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
    fill = {"complainant": pick(k.complainants, 24), "camp": pick(k.camps, 32), "loss": pick(k.losses, 64)}
    title, why, place = (s.format(**fill) for s in (k.title, k.why, k.place))
    where = tuple(w.format(**fill) for w in k.where)
    culprit = pick(k.culprits, 40) or None
    look = (k.culprit_looks[(seed >> 40) % len(k.culprits)]
            if k.culprits and len(k.culprit_looks) == len(k.culprits) else None)
    suspect = None
    if k.suspects and ((seed >> 48) % 1000) / 1000 < k.suspect_chance:
        suspect = culprit = pick(k.suspects, 56)
        why += f". Word is it was {suspect}"
        look = None                                 # the word names them; the complainant thinks it was them
    payload = {"org": org.key, "key": k.key, "title": title, "why": why, "instructions": k.instructions.format(**fill),
               "where": list(where), "need": k.need, "outcome": k.outcome, "place": place,
               "shift_hours": k.shift_hours, "min_rank": k.min_rank, "merit": k.merit, "culprit": culprit,
               "expires_gt": opened + k.lasts_days, "day": day, "suspect": suspect, "charge": k.charge,
               "complainant": fill["complainant"] or None, "complaint": k.complaint.format(**fill) if k.complaint else "",
               "look": look, "memory": k.memory.format(**fill) if k.memory else ""}
    event_id = S.event(db, "incident", [], payload, opened, where[0] if where else org.hold, [])
    inc = get(db, event_id)
    who = complainant_id(db, inc)
    if who is not None:                             # met already: they remember going to the guard from now on
        remember_complaint(db, inc, who)
    return inc


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


# ---- the complainant: the townsperson who reported it remembers doing so ----

COMPLAINT_DAYS = 3.0       # how long after it is over the complainant still has it on their mind


def complainant_id(db, inc: Incident) -> int | None:
    """The complainant's NPC id, if they are known to the world (they have spoken through Mantella before) and
    the name is not shared by anyone else."""
    if not inc.complainant:
        return None
    rows = db.execute("SELECT npc_id FROM npc WHERE is_player = 0 AND display_name = ? COLLATE NOCASE",
                      (inc.complainant,)).fetchall()
    if len(rows) != 1 or S.is_child(db, rows[0][0]):
        return None
    return rows[0][0]


def remember_complaint(db, inc: Incident, npc_id: int) -> bool:
    """Give the complainant the memory of reporting it to the guard (once per incident; rolls back with saves).
    Returns whether a memory was written."""
    if db.execute("SELECT 1 FROM v_visible_memory WHERE owner_id = ? AND event_id = ? LIMIT 1",
                  (npc_id, inc.event_id)).fetchone():
        return False
    org = O.by_key(inc.org_key)
    guard = org.name if org else "the guard"
    text = inc.memory or f"I reported trouble to {guard}: {inc.why}."
    if inc.look:
        text += f" All I saw of the culprit was {inc.look}."
    elif inc.suspect:
        text += f" I think it was {inc.suspect}."
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, confidence, event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (S._timeline(db), npc_id, "episodic", text, "[]", inc.where[0] if inc.where else None, inc.opened_gt, 0.7,
         -0.4, 0.5, "perception", "experienced", 1.0, inc.event_id)).lastrowid
    db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, inc.event_id))
    db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
               (inc.event_id, npc_id, "participated"))
    return True


@dataclass
class Complaint:
    incident: Incident
    state: str                      # 'open' | 'arrested' | 'cleared' | 'went cold'
    member_id: int | None = None    # the guard sent to it (taken or dealt with), if any
    closed_gt: float | None = None


def _closing(db, event_id: int) -> tuple[str, float, list[int]] | None:
    row = db.execute("SELECT payload_json, game_time, actors_json FROM v_visible_event WHERE type ="
                     " 'guard.incident_closed' AND json_extract(payload_json, '$.incident') = ?"
                     " ORDER BY game_time LIMIT 1", (event_id,)).fetchone()
    if not row:
        return None
    return json.loads(row[0]).get("outcome", "closed"), row[1], json.loads(row[2] or "[]")


def _taker(db, event_id: int) -> int | None:
    row = db.execute("SELECT assignee_id FROM v_task WHERE template LIKE ? AND is_reset = 0 AND state IN"
                     " ('accepted','in_progress','paused','completed') ORDER BY task_id DESC LIMIT 1",
                     (f"{PREFIX}%:{event_id}",)).fetchone()
    return row[0] if row else None


def complaints_by(db, name: str, now: float) -> list[Complaint]:
    """Incidents ``name`` reported that are still open, or over within the last few days (newest first)."""
    if not name:
        return []
    rows = db.execute("SELECT event_id, payload_json, game_time FROM v_visible_event WHERE type = 'guard.incident'"
                      " AND game_time <= ? AND game_time >= ? ORDER BY game_time DESC, event_id DESC",
                      (now + 1e-9, now - 14)).fetchall()
    out = []
    for row in rows:
        inc = _row(row)
        if not inc.complainant or inc.complainant.casefold() != name.casefold():
            continue
        closing = _closing(db, inc.event_id)
        taker = _taker(db, inc.event_id)
        if closing is None:
            if inc.expires_gt <= now and taker is None:
                if now - inc.expires_gt > COMPLAINT_DAYS:
                    continue
                out.append(Complaint(inc, "went cold", None, inc.expires_gt))
            else:
                out.append(Complaint(inc, "open", taker))
            continue
        outcome, gt, actors = closing
        if now - gt > COMPLAINT_DAYS:
            continue
        member = actors[0] if outcome in OUTCOMES and actors else None
        out.append(Complaint(inc, outcome if outcome in OUTCOMES else "went cold", member, gt))
    return out
