"""G2e: shift encounters, so a guard shift is almost never empty.

Each routine duty is split into watch windows (by default near its start, middle and end). Whether a window
brings something is rolled with a seed when the duty is given, and the odds climb while the shift has been
quiet: with the default 50% / 80% / 90% a shift ends with nothing to report about one time in a hundred.
After the first thing happens the rest only come up at ``extra_chance``, so most shifts get one to three
small matters rather than one big drama. Nothing here creates crime, danger or a fight: those stay with the
incident rolls (``incident.py``).

The game is told the windows (times, and for each whether someone should come over or the member should
notice something). When a window falls due on the member's shift and no conversation is running, the game
picks a nearby adult who is awake, not hostile and has not come over in the last few days, has them walk
up and starts a Mantella conversation. If nobody fits, the member notices something instead (an
observation: a notification, nobody spawned, no conversation forced). Either way it sends a ``watch``
record (``perception/records.py``), which lands here:

- **approach**: a ``guard.encounter`` event naming who came over and why. The reason is picked from the
  categories in the duties file, weighted by the place, the hour, open incidents, the member's rank and
  who it is (a fellow guard brings word; the member's own superior comes to inspect them; someone who came
  over before comes back about it). Their prompt says why they came; the player answers in their own words.
  The member answering at all counts as dealing with it (``guard.encounter_answered``).
- **observe**: a ``guard.observation`` event with what was noticed.

When the member reports back, ``summary`` gathers the shift (encounters, observations, matters for the
superior, trouble still open) into the report: the superior's prompt hears it and a ``guard.shift_summary``
event keeps the counts for the service record and, later, promotions (G3).

Switch off with ``MANTELLA_LW_GUARD_ENCOUNTERS=0``.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
from dataclasses import dataclass
from importlib import resources

from livingworld.guard import orgs as O, profiles, store as S
from livingworld.work import tasks as T

ENV = "MANTELLA_LW_GUARD_ENCOUNTERS"
APPROACH, OBSERVE = "approach", "observe"
FRESH_DAYS = 0.06          # an encounter shapes the approaching NPC's prompt for about an hour and a half
NIGHT = (22, 5)


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    weight: float
    reasons: tuple[str, ...]
    refer: bool = False                     # usually a matter for the superior
    min_rank: str | None = None
    night: float = 1.0                      # weight multiplier at night
    places: tuple[tuple[str, float], ...] = ()   # (word in the location, multiplier)
    when_incident: float = 1.0              # multiplier while trouble is open
    who: str = "anyone"                     # anyone | guard | superior | returning


@dataclass(frozen=True)
class Observation:
    text: str
    refer: bool = False
    hours: tuple[int, int] | None = None


@dataclass(frozen=True)
class Config:
    windows: tuple[float, ...] = (0.15, 0.5, 0.85)
    first_chance: tuple[float, ...] = (0.5, 0.8, 0.9)
    extra_chance: float = 0.35
    approach_share: float = 0.75
    npc_cooldown_days: float = 3.0
    merit_each: float = 0.25
    merit_cap: float = 1.0
    categories: tuple[Category, ...] = ()
    observations: tuple[Observation, ...] = ()

    def category(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)


@functools.lru_cache(maxsize=None)
def config(org_key: str) -> Config:
    org = O.by_key(org_key)
    if org is None or not org.duty_file:
        return Config()
    raw = json.loads(resources.files("livingworld.guard").joinpath("data", org.duty_file).read_text(encoding="utf-8"))
    e = raw.get("encounters") or {}
    cats = tuple(Category(c["key"], c.get("label", c["key"]), float(c.get("weight", 1)), tuple(c.get("reasons") or ()),
                          bool(c.get("refer")), c.get("min_rank"), float(c.get("night", 1)),
                          tuple((str(k).lower(), float(v)) for k, v in (c.get("places") or {}).items()),
                          float(c.get("when_incident", 1)), c.get("who", "anyone"))
                 for c in e.get("categories") or ())
    obs = tuple(Observation(o["text"], bool(o.get("refer")), tuple(o["hours"]) if o.get("hours") else None)
                for o in e.get("observations") or ())
    keep = {k: e[k] for k in ("extra_chance", "approach_share", "npc_cooldown_days", "merit_each", "merit_cap")
            if k in e}
    if "windows" in e:
        keep["windows"] = tuple(float(x) for x in e["windows"])
    if "first_chance" in e:
        keep["first_chance"] = tuple(float(x) for x in e["first_chance"])
    return Config(categories=cats, observations=obs, **keep)


def _roll(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)


def _chance(seed: int) -> float:
    return (seed % 10_000) / 10_000


def _hour(gt: float) -> int:
    return int((gt % 1.0) * 24)


def _night(gt: float) -> bool:
    h = _hour(gt)
    return h >= NIGHT[0] or h < NIGHT[1]


def _in(hours: tuple[int, int] | None, h: int) -> bool:
    if hours is None:
        return True
    a, b = hours
    return a <= h < b if a < b else (h >= a or h < b)


# ---- the plan the game is given ----

@dataclass(frozen=True)
class Window:
    win: int
    at: float
    kind: str                 # approach (someone comes over; falls back to observe) | observe
    observation: str          # what the member notices if nobody comes over


@dataclass(frozen=True)
class Plan:
    task_id: int
    end: float
    windows: tuple[Window, ...]

    def signature(self) -> tuple:
        return (self.task_id, round(self.end, 5), tuple((w.win, round(w.at, 5), w.kind) for w in self.windows))


def wants_windows(d) -> bool:
    """Routine duties get watch windows; trouble (incidents) is its own activity."""
    return d is not None and d.incident_id is None and d.need == "seen" and not d.training


def windows(org: O.Org, task_id: int, start: float, shift_hours: float) -> list[Window]:
    """Every window that comes up for this duty (seeded by the task: the same each time it is asked)."""
    cfg = config(org.key)
    out: list[Window] = []
    quiet = True
    for i, frac in enumerate(cfg.windows):
        seed = _roll(org.key, "watch", task_id, i)
        p = (cfg.first_chance[min(i, len(cfg.first_chance) - 1)] if cfg.first_chance else 0.5) if quiet \
            else cfg.extra_chance
        if _chance(seed) >= p:
            continue
        quiet = False
        at = start + frac * shift_hours / 24.0
        kind = APPROACH if _chance(seed >> 20) < cfg.approach_share else OBSERVE
        out.append(Window(i, at, kind, observation(org, task_id, i, at).text))
    return out


def observation(org: O.Org, task_id: int, win: int, at: float) -> Observation:
    cfg = config(org.key)
    fits = [o for o in cfg.observations if _in(o.hours, _hour(at))] or list(cfg.observations)
    if not fits:
        return Observation("Nothing out of the ordinary.")
    return fits[_roll(org.key, "obs", task_id, win) % len(fits)]


def plan(db, org: O.Org, task: T.Task, d, start: float) -> Plan:
    """The windows still to come for the duty in hand (those already used are left out)."""
    used = {r[0] for r in db.execute(
        "SELECT json_extract(payload_json, '$.win') FROM v_visible_event WHERE type IN ('guard.encounter',"
        " 'guard.observation') AND json_extract(payload_json, '$.task') = ?", (task.task_id,))}
    ws = tuple(w for w in windows(org, task.task_id, start, d.shift_hours) if w.win not in used)
    return Plan(task.task_id, start + d.shift_hours / 24.0, ws)


def soon(db, org: O.Org, task: T.Task, now: float, end: float) -> Plan:
    """Testing ("debug guard watch"): windows a few game minutes from now, two approaches then a notice."""
    used = {r[0] for r in db.execute(
        "SELECT json_extract(payload_json, '$.win') FROM v_visible_event WHERE type IN ('guard.encounter',"
        " 'guard.observation') AND json_extract(payload_json, '$.task') = ?", (task.task_id,))}
    ws = []
    for i in range(len(config(org.key).windows)):
        if i in used:
            continue
        at = now + 0.004 * (len(ws) + 1)               # about six game minutes apart
        ws.append(Window(i, at, APPROACH if i < 2 else OBSERVE, observation(org, task.task_id, i, at).text))
    return Plan(task.task_id, max(end, now + 0.05), tuple(ws))


# ---- what the game reports ----

@dataclass
class Encounter:
    event_id: int
    npc_id: int | None
    category: str
    label: str
    reason: str
    refer: bool
    task_id: int
    gt: float


def _reason(db, event_id: int) -> dict:
    """Why someone came over is kept in a ``guard.encounter_reason`` event (world events are append-only)."""
    r = db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'guard.encounter_reason' AND"
                   " json_extract(payload_json, '$.encounter') = ? ORDER BY event_id DESC LIMIT 1",
                   (event_id,)).fetchone()
    return json.loads(r[0]) if r else {}


def _enc(row, db=None) -> Encounter:
    p = json.loads(row["payload_json"])
    if db is not None:
        p.update({k: v for k, v in _reason(db, row["event_id"]).items() if k != "encounter"})
    actors = json.loads(row["actors_json"])
    return Encounter(row["event_id"], actors[1] if len(actors) > 1 else None, p.get("category", ""), p.get("label", ""),
                     p.get("reason", ""), bool(p.get("refer")), int(p.get("task") or 0), row["game_time"])


def _open_incident_text(db, org: O.Org, now: float) -> str | None:
    from livingworld.guard import incident as INC
    trouble = INC.open_incidents(db, org, now)
    return trouble[0].title if trouble else None


def _weights(db, org: O.Org, svc: S.Service, npc_id: int, boss: int | None, location: str | None, now: float,
             cfg: Config, this_event: int | None = None) -> tuple[list[tuple[Category, float]], dict]:
    loc = (location or "").lower()
    is_guard = profiles.get(db, npc_id) is not None
    last = db.execute("SELECT event_id, payload_json, actors_json, game_time FROM v_visible_event WHERE type ="
                      " 'guard.encounter' AND json_extract(actors_json, '$[1]') = ? AND json_extract(actors_json,"
                      " '$[0]') = ? AND event_id != ?"
                      " ORDER BY game_time DESC, event_id DESC LIMIT 1", (npc_id, svc.member_id, this_event or -1)).fetchone()
    trouble = _open_incident_text(db, org, now)
    fill = {"incident": trouble or "the latest trouble", "last": "", "post": "", "superior": "",
            "hold": org.hold, "guard": org.name}
    if last is not None:
        fill["last"] = (_enc(last, db).label or "something").lower()
    out = []
    for c in cfg.categories:
        if c.who == "superior":
            w = c.weight if npc_id == boss else 0.0
        elif c.who == "guard":
            w = c.weight if is_guard and npc_id != boss else 0.0
        elif c.who == "returning":
            w = c.weight if last is not None and now - last["game_time"] <= 14 else 0.0
        else:
            w = 0.0 if (is_guard or npc_id == boss) else c.weight
        if w <= 0:
            continue
        if c.min_rank and org.rank_index(svc.rank_key) < org.rank_index(c.min_rank):
            w *= 0.15
        if _night(now):
            w *= c.night
        for word, mult in c.places:
            if word in loc:
                w *= mult
                break
        if trouble:
            w *= c.when_incident
        elif c.when_incident > 1 and "{incident}" in " ".join(c.reasons):
            w *= 0.3
        out.append((c, w))
    return out, fill


def record_watch(db, member_id: int, npc_id: int | None, task_id: int, win: int, via: str, gt: float,
                 location: str | None) -> int | None:
    """Store what one watch window brought (the game's ``watch`` record). Returns the event id, or None."""
    from livingworld.guard import duty as DU
    svc = S.current(db, member_id)
    org = O.by_key(svc.org_key) if svc else None
    if org is None or not enabled():
        return None
    task = T.get(db, task_id) if task_id else None
    if task is None or task.assignee_id != member_id or task.state not in T.TAKEN:
        return None
    from livingworld.store.minors import is_child
    if npc_id is not None and is_child(db, npc_id):
        npc_id = None          # children are left out: the member notices something instead
    dup = db.execute("SELECT 1 FROM v_visible_event WHERE type IN ('guard.encounter', 'guard.observation') AND"
                     " json_extract(payload_json, '$.task') = ? AND json_extract(payload_json, '$.win') = ? LIMIT 1",
                     (task_id, win)).fetchone()
    if dup is not None:
        return None
    if via == APPROACH and npc_id is not None and npc_id != member_id:
        # why they came is worked out with their first prompt, once the game has said whether they are a guard
        return S.event(db, "encounter", [member_id, npc_id],
                       {"org": org.key, "task": task_id, "win": win, "category": None, "label": "", "reason": "",
                        "refer": False}, gt, location, [])
    obs = observation(org, task_id, win, gt)
    return S.event(db, "observation", [member_id], {"org": org.key, "task": task_id, "win": win, "text": obs.text,
                                                    "refer": obs.refer}, gt, location, [])


def _resolve(db, row) -> None:
    """Pick why someone came over (category and reason) and keep it on the event."""
    from livingworld.guard import duty as DU
    p = json.loads(row["payload_json"])
    member_id, npc_id = json.loads(row["actors_json"])[:2]
    svc = S.current(db, member_id)
    org = O.by_key(svc.org_key) if svc else None
    if org is None:
        return
    task = T.get(db, int(p.get("task") or 0))
    d = DU.duty_for(db, task) if task else None
    boss = DU.superior_id(db, org, svc)
    cfg = config(org.key)
    weights, fill = _weights(db, org, svc, npc_id, boss, row["location"], row["game_time"], cfg, row["event_id"])
    total = sum(w for _, w in weights)
    if total <= 0:
        p.update(category="other", label="Someone came over", reason="you have something to ask the guard")
    else:
        seed = _roll(org.key, "enc", p.get("task"), p.get("win"), npc_id)
        x = _chance(seed) * total
        pick = weights[-1][0]
        for c, w in weights:
            if x < w:
                pick = c
                break
            x -= w
        fill["post"] = d.title if d else "their post"
        person = org.superior(svc.rank_key)
        fill["superior"] = person.name if person else "their superior"
        reason = pick.reasons[(seed >> 24) % len(pick.reasons)].format(**fill) if pick.reasons else pick.label
        p.update(category=pick.key, label=pick.label, reason=reason, refer=pick.refer)
    S.event(db, "encounter_reason", [member_id, npc_id], {"encounter": row["event_id"], **{
        k: p.get(k) for k in ("category", "label", "reason", "refer")}}, row["game_time"], row["location"], [])


def fresh_for(db, npc_id: int, member_id: int, now: float) -> Encounter | None:
    """The encounter that brought ``npc_id`` over to the member just now, if any."""
    row = db.execute("SELECT event_id, payload_json, actors_json, game_time FROM v_visible_event WHERE type ="
                     " 'guard.encounter' AND json_extract(actors_json, '$[0]') = ? AND json_extract(actors_json, '$[1]')"
                     " = ? AND game_time <= ? AND game_time >= ? ORDER BY game_time DESC, event_id DESC LIMIT 1",
                     (member_id, npc_id, now + 1e-6, now - FRESH_DAYS)).fetchone()
    if row is None:
        return None
    if not _reason(db, row["event_id"]):
        _resolve(db, db.execute("SELECT event_id, payload_json, actors_json, game_time, location FROM world_event"
                                " WHERE event_id = ?", (row["event_id"],)).fetchone())
    return _enc(row, db)


def answered(db, enc: Encounter, member_id: int, gt: float, location: str | None) -> bool:
    """The member spoke with whoever came over: the matter counts as dealt with (once)."""
    done = db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.encounter_answered' AND"
                      " json_extract(payload_json, '$.encounter') = ? LIMIT 1", (enc.event_id,)).fetchone()
    if done is not None:
        return False
    S.event(db, "encounter_answered", [member_id, enc.npc_id], {"encounter": enc.event_id, "task": enc.task_id,
                                                                 "category": enc.category}, gt, location, [])
    return True


# ---- the shift's summary ----

@dataclass
class Summary:
    encounters: list[tuple[str, str, bool, bool]]   # (who, what, answered, refer)
    observations: list[tuple[str, bool]]            # (text, refer)
    trouble: list[str]                              # open incidents nobody has dealt with

    @property
    def answered(self) -> int:
        return sum(1 for e in self.encounters if e[2])

    @property
    def attention(self) -> list[str]:
        return ([f"{what} ({who})" for who, what, _, refer in self.encounters if refer]
                + [text for text, refer in self.observations if refer])

    @property
    def quiet(self) -> bool:
        return not self.encounters and not self.observations

    def counts(self) -> str:
        parts = []
        if self.encounters:
            parts.append(f"{len(self.encounters)} encounter{'s' if len(self.encounters) != 1 else ''}")
        if self.observations:
            parts.append(f"{len(self.observations)} observation{'s' if len(self.observations) != 1 else ''}")
        if self.attention:
            parts.append(f"{len(self.attention)} for your superior")
        return ", ".join(parts) or "a quiet shift"

    def text(self) -> str:
        if self.quiet:
            out = "It was a quiet shift: nothing to report."
        else:
            bits = []
            if self.encounters:
                bits.append("people who came to them: " + "; ".join(
                    f"{who}, {what}" + ("" if ok else " (not dealt with)") for who, what, ok, _ in self.encounters))
            if self.observations:
                bits.append("what they noticed: " + "; ".join(t for t, _ in self.observations))
            out = "On the shift, " + ". ".join(bits) + "."
        if self.attention:
            out += " Matters for the superior to decide: " + "; ".join(self.attention) + "."
        if self.trouble:
            out += " Still open: " + "; ".join(self.trouble) + "."
        return out


def summary(db, org: O.Org, task: T.Task, now: float) -> Summary:
    rows = db.execute("SELECT event_id, type, payload_json, actors_json, game_time FROM v_visible_event WHERE type IN"
                      " ('guard.encounter', 'guard.observation') AND json_extract(payload_json, '$.task') = ?"
                      " ORDER BY game_time, event_id", (task.task_id,)).fetchall()
    encs, obs = [], []
    for r in rows:
        if r["type"] == "guard.observation":
            p = json.loads(r["payload_json"])
            obs.append((p.get("text", ""), bool(p.get("refer"))))
            continue
        e = _enc(r, db)
        who = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (e.npc_id,)).fetchone() if e.npc_id else None
        g = profiles.get(db, e.npc_id) if e.npc_id else None
        name = (g.given_name if g and g.given_name else None) or (who[0] if who else "someone")
        ok = db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.encounter_answered' AND"
                        " json_extract(payload_json, '$.encounter') = ? LIMIT 1", (e.event_id,)).fetchone() is not None
        encs.append((name, (e.label or "came over").lower(), ok, e.refer))
    from livingworld.guard import incident as INC
    trouble = [i.title for i in INC.open_incidents(db, org, now, free_only=True)]
    return Summary(encs, obs, trouble[:2])


def file_summary(db, org: O.Org, svc: S.Service, task: T.Task, by: int | None, now: float, location: str | None,
                 witnesses: list[int] | None = None, source: str = "sim") -> Summary:
    """Keep the shift's summary with the report (counts for the record) and add a little merit for the
    people the member dealt with."""
    s = summary(db, org, task, now)
    cfg = config(org.key)
    S.event(db, "shift_summary", [svc.member_id, by],
            {"org": org.key, "service_id": svc.service_id, "task": task.task_id, "encounters": len(s.encounters),
             "answered": s.answered, "observations": len(s.observations), "attention": len(s.attention),
             "text": s.text()}, now, location, witnesses, source)
    bonus = min(cfg.merit_cap, cfg.merit_each * s.answered)
    if bonus > 0:
        S.change(db, svc, f"dealt with {s.answered} matter{'s' if s.answered != 1 else ''} on shift", now,
                 d_merit=bonus, source=source)
    return s


def helped(db, svc: S.Service) -> int:
    """People the member dealt with on shift, over their whole service (for the record)."""
    return db.execute("SELECT COALESCE(SUM(json_extract(payload_json, '$.answered')), 0) FROM v_visible_event WHERE"
                      " type = 'guard.shift_summary' AND json_extract(payload_json, '$.service_id') = ?",
                      (svc.service_id,)).fetchone()[0] or 0
