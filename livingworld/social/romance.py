"""Romance between NPCs, off screen (Living World R1). Flag: MANTELLA_LW_ROMANCE=1 (off by default).

Runs like gossip: when the server is next contacted, each community is caught up to the current game
time in seeded steps (one per quarter day), so the same save always tells the same story. Numbers only,
no LLM, and nothing here plays an animation: every encounter is recorded with ``mode='offscreen'``.

One step, in order:
1. once a game day: attraction drifts toward each pair's "spark" (most pairs have none), jealousy fades,
   and bonds may break up or (after a while) reconcile;
2. people who are taken may flirt with someone else where others can see (seen by a partner, it hurts);
3. bonded pairs may spend an evening together or, at night, private time; every one of those goes
   through ``consent.decide`` for each person at each phase, and needs a private enough place;
4. someone with strong private interest may ask for the next step: courting, or (rarely, and only when
   both say yes) an affair; a no is respected with a cooldown, and two nos end the asking.

What an encounter changes: satisfaction for each person by how it went for them, and intimacy and
commitment only inside a bond. It never raises attraction or trust. Discovery is never scripted: it
comes only from people who noticed (``event_witness``) and from gossip about it (claims).
Children are never members of a community, so they never appear here (``store/minors.py``).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass

from livingworld.social import consent
from livingworld.social.seed import _local_id, romance_traits

ROMANCE_ENV = "MANTELLA_LW_ROMANCE"

STEP_DAYS = 0.25
MAX_STEPS = 120
TICK_TYPE = "romance_tick"

DEFAULT_TRAITS = {"romantic": 0.5, "drive": 0.5, "monogamy": 0.8, "jealous": 0.5, "flirtatious": 0.3, "shy": 0.5,
                  "modesty": 0.5, "trusting": 0.5, "loyal": 0.5, "intimacy_boundary": "committed_only"}

ACTIVE = ("courting", "dating", "partners", "betrothed", "married", "affair")
NEXT_STAGE = {"courting": "dating", "dating": "partners", "partners": "betrothed", "betrothed": "married"}
MIN_DAYS_AT = {"courting": 5, "dating": 15, "partners": 30, "betrothed": 10}
NEEDS = {"dating": (0.25, 0.0), "partners": (0.45, 0.3), "betrothed": (0.6, 0.5), "married": (0.65, 0.6)}  # intimacy, commitment
STATUS_FOR_STAGE = {"courting": "courting", "dating": "courting", "partners": "partner", "betrothed": "partner",
                    "married": "spouse", "ended": "ex"}

# pace (slow and realistic by default)
ASK_INTEREST = 0.45            # private interest needed before asking to court
ACCEPT_INTEREST = 0.35         # the other's own interest needed to say yes
AFFAIR_INTEREST = 0.3
ASK_PER_DAY = 0.12
AFFAIR_PER_DAY = 0.04
STAGE_UP_PER_DAY = 0.1
DATE_PER_DAY = 0.3
INTIMATE_PER_DAY = 0.15        # times (drive a + drive b); about one to three nights a week for a keen couple
FLIRT_PER_DAY = 0.1            # times flirtatious, for people who are taken (Hod: about once a fortnight)
PERSON_STAGE_GAP = 3.0         # days between bond changes for one person
COMMUNITY_NEW_BOND_GAP = 7.0   # days between new courtships or affairs in one community
DECLINE_COOLDOWN = 7.0
GIVE_UP_DAYS = 30.0
RECONCILE_AFTER = 14.0
RESIDENT_PENALTY = 0.2         # another adult living there
CHILD_PENALTY = 0.15           # a child living there
PRIVATE_ENOUGH = 0.7
SATISFACTION_BASE = 0.2        # where a bond's feeling settles back to, day by day
JEALOUSY_FADE = 0.015          # per day: hurt lingers for weeks


def romance_enabled() -> bool:
    return os.environ.get(ROMANCE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CatchUp:
    community: str
    steps: int = 0
    happenings: int = 0
    ms: float = 0.0


# ---- dice ----

def _rng(*parts) -> random.Random:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "little"))


def _chance(per_day: float, step: float) -> float:
    return 1 - (1 - min(0.95, max(0.0, per_day))) ** step


# ---- reading ----

def traits(db, npc_id: int) -> dict:
    row = db.execute("SELECT plugin, base_form_id, sex FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    out = dict(DEFAULT_TRAITS)
    sex = row[2] if row else None
    if row:
        seeded = romance_traits().get((row[0], _local_id(row[1].replace("0x", "").replace("0X", ""))))
        if seeded:
            out.update(seeded)
            sex = seeded.get("sex", sex)
    out["sex"] = sex
    if "attracted_to" not in out:
        out["attracted_to"] = [{"male": "female", "female": "male"}[sex]] if sex in ("male", "female") else []
    return out


def rel(db, a: int, b: int) -> dict:
    row = db.execute("SELECT r.*, base.attraction AS base_attraction FROM v_relationship r"
                     " JOIN relationship base ON base.from_id = r.from_id AND base.to_id = r.to_id"
                     " WHERE r.from_id = ? AND r.to_id = ?", (a, b)).fetchone()
    return dict(row) if row else {}


def bond(db, a: int, b: int):
    lo, hi = min(a, b), max(a, b)
    return db.execute("SELECT * FROM v_romance_bond WHERE a_id = ? AND b_id = ?", (lo, hi)).fetchone()


def active_bonds(db, npc: int) -> list:
    return db.execute(f"SELECT * FROM v_romance_bond WHERE (a_id = ? OR b_id = ?) AND stage IN ({_marks(ACTIVE)})",
                      (npc, npc, *ACTIVE)).fetchall()


def partner_of(row, npc: int) -> int:
    return row["b_id"] if row["a_id"] == npc else row["a_id"]


def _marks(items) -> str:
    return ",".join("?" * len(items))


def _name(db, npc: int) -> str:
    return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc,)).fetchone()[0]


def attracted(t_from: dict, t_to: dict) -> bool:
    return t_to.get("sex") in (t_from.get("attracted_to") or [])


def freedom(db, npc: int, t: dict, other: int) -> float:
    """How free npc feels to pursue someone new: 1 when unattached. When taken, low monogamy,
    low commitment, an unhappy bond and jealousy of their partner all loosen it."""
    taken = [b for b in active_bonds(db, npc) if partner_of(b, npc) != other and b["stage"] != "affair"]
    if not taken:
        return 1.0
    f = 1.0
    for b in taken:
        r = rel(db, npc, partner_of(b, npc))
        hurt = max(0.0, -r.get("satisfaction", 0.0)) + r.get("jealousy", 0.0)
        f *= (1 - t["monogamy"]) * (1 - 0.6 * r.get("commitment", 0.0)) * (1 + hurt)
    return min(1.0, f)


def interest(db, a: int, b: int, ta: dict, tb: dict) -> float:
    if not attracted(ta, tb):
        return 0.0
    r = rel(db, a, b)
    if not r:
        return 0.0
    return r["attraction"] * (0.5 + ta["romantic"]) * freedom(db, a, ta, b)


# ---- writing ----

def _ensure_rel(db, a: int, b: int, gt: float) -> None:
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, familiarity, updated_gt)"
               " VALUES (?, ?, 'acquaintance', 0.4, ?)", (a, b, gt))


def _delta(db, timeline: int, a: int, b: int, deltas: dict, reason: str, gt: float, event_id: int | None = None) -> None:
    deltas = {k: round(v, 4) for k, v in deltas.items() if abs(v) >= 0.0005}
    if not deltas:
        return
    _ensure_rel(db, a, b, gt)
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)", (timeline, a, b, json.dumps(deltas), event_id, reason, gt))


def _event(db, timeline: int, etype: str, location: str | None, actors: list[int], payload: dict, gt: float,
           witnesses: dict[int, str] | None = None) -> int:
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?,?,?,?,?, 'sim', ?)", (timeline, etype, location, json.dumps(actors), json.dumps(payload), gt)).lastrowid
    for npc, how in (witnesses or {}).items():
        db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)", (event_id, npc, how))
    return event_id


def _remember(db, timeline: int, owner: int, content: str, about: list[int], gt: float, importance: float,
              valence: float, event_id: int | None, location: str | None, interpretation: str | None = None) -> None:
    db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
               " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
               " VALUES (?, ?, 'episodic', ?, ?, ?, ?, ?, ?, ?, 'perception', 'experienced', ?, 1.0, ?)",
               (timeline, owner, content, json.dumps(about), location, gt, importance, valence, abs(valence),
                interpretation, event_id))


def set_bond(db, timeline: int, a: int, b: int, stage: str, reason: str, gt: float, location: str | None,
             secret: bool = False, witnesses: dict[int, str] | None = None) -> int:
    lo, hi = min(a, b), max(a, b)
    current = bond(db, lo, hi)
    exclusive = 1 if stage in ("partners", "betrothed", "married") else 0
    event_id = _event(db, timeline, "romance.bond_changed", location, [a, b],
                      {"stage_from": current["stage"] if current else None, "stage_to": stage, "reason": reason,
                       "secret": secret},
                      gt, {a: "participated", b: "participated", **(witnesses or {})})
    db.execute("INSERT INTO romance_bond_change (timeline_id, a_id, b_id, stage_from, stage_to, exclusive, secret,"
               " reason, event_id, game_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (timeline, lo, hi, current["stage"] if current else None, stage, exclusive, int(secret), reason,
                event_id, gt))
    if not secret and stage in STATUS_FOR_STAGE:
        for x, y in ((a, b), (b, a)):
            _ensure_rel(db, x, y, gt)
            db.execute("UPDATE relationship SET status = ? WHERE from_id = ? AND to_id = ?", (STATUS_FOR_STAGE[stage], x, y))
    return event_id


# ---- places ----

def place_privacy(db, place, a: int, b: int) -> float:
    residents = set(json.loads(place["residents_json"] or "[]"))
    if place["kind"] == "home_bedroom" and not residents & {a, b}:
        return 0.0              # not their home
    value = place["privacy"] - RESIDENT_PENALTY * len(residents - {a, b})
    if place["child_in_home"]:
        value -= CHILD_PENALTY
    return round(max(0.0, value), 3)


def best_place(db, location: str | None, a: int, b: int, kind: str, gt: float):
    """(place row, privacy) of the most private free place for this pair, or (None, 0)."""
    best, best_privacy = None, 0.0
    for place in db.execute("SELECT * FROM place WHERE location IS ? OR ? IS NULL ORDER BY place_key", (location, location)):
        if kind == "intimate" and place["kind"] not in ("home_bedroom", "inn_room", "private_room"):
            continue
        if kind != "intimate" and place["kind"] in ("home_bedroom",):
            continue
        if place["kind"] == "inn_room" and db.execute(
                "SELECT 1 FROM v_encounter WHERE place_key = ? AND outcome = 'completed' AND game_time > ? LIMIT 1",
                (place["place_key"], gt - 0.5)).fetchone():
            continue            # already taken tonight
        privacy = place_privacy(db, place, a, b)
        if privacy > best_privacy:
            best, best_privacy = place, privacy
    return best, best_privacy


def _noticers(db, place, a: int, b: int, rng: random.Random, members: list[int]) -> dict[int, str]:
    """Who notices, off screen: other adults living there may hear; an inn's staff see who takes a room."""
    out = {}
    for r in json.loads(place["residents_json"] or "[]"):
        if r not in (a, b) and rng.random() < 0.3:
            out[r] = "heard"
    if place["staff_household_id"] is not None:
        for (s,) in db.execute("SELECT m.npc_id FROM membership m JOIN npc n ON n.npc_id = m.npc_id"
                               " WHERE m.community_id = ? AND n.is_child = 0 ORDER BY m.npc_id",
                               (place["staff_household_id"],)):
            if s not in (a, b) and rng.random() < 0.4:
                out[s] = "saw"
    return out


# ---- what people make of what they saw or heard (called from claims) ----

def on_learned(db, timeline: int, holder: int, claim: dict, believed: float, gt: float, event_id: int | None = None) -> None:
    """A partner learns their partner flirted with, or spent the night with, someone else."""
    act = claim["act"]
    if act not in ("flirted", "spent_night", "caught_with"):
        return
    subject, obj = claim["subject_id"], claim.get("object_id")
    pairs = ((subject, obj),) if act == "flirted" else ((subject, obj), (obj, subject))
    for cheater, other in pairs:
        if cheater is None or cheater == holder or other == holder:
            continue
        b = bond(db, holder, cheater)
        if not b or b["stage"] not in ACTIVE or b["stage"] == "affair":
            continue
        if act == "spent_night" and bond(db, subject, obj) and bond(db, subject, obj)["stage"] in ACTIVE \
                and not bond(db, subject, obj)["secret"]:
            continue            # an open bond the holder already lives with
        t = traits(db, holder)
        if act == "flirted":
            deltas = {"jealousy": 0.15 * (0.5 + t["jealous"]) * believed, "satisfaction": -0.08 * believed,
                      "trust": -0.02 * believed}
        else:
            deltas = {"jealousy": 0.4 * (0.5 + t["jealous"]) * believed, "satisfaction": -0.35 * believed,
                      "trust": -0.3 * believed, "commitment": -0.15 * believed}
        _delta(db, timeline, holder, cheater, deltas, f"learned {_name(db, cheater)} {act} someone else", gt, event_id)


# ---- the simulation ----

def _last_tick(db, community: str) -> float | None:
    return db.execute("SELECT MAX(game_time) FROM v_visible_event WHERE type = ? AND"
                      " json_extract(payload_json, '$.community') = ?", (TICK_TYPE, community)).fetchone()[0]


def catch_up(db, community_key: str, now_gt: float, budget_ms: float | None = None) -> CatchUp:
    """Advance romance in one community up to ``now_gt``. Call inside a transaction."""
    from livingworld.social.gossip import _members
    started = time.perf_counter()
    result = CatchUp(community_key)
    row = db.execute("SELECT community_id, location FROM community WHERE key = ?", (community_key,)).fetchone()
    if not row:
        return result
    community_id, location = row
    members = _members(db, community_id)
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    _bootstrap(db, timeline, members, location, now_gt)   # also covers seeded spouses first met later
    last = _last_tick(db, community_key)
    if last is None:
        _tick(db, timeline, community_key, location, now_gt)
        return result
    if now_gt - last < STEP_DAYS:
        return result
    step = max(STEP_DAYS, (now_gt - last) / MAX_STEPS)
    t = last
    ts = {m: traits(db, m) for m in members}
    while t + step <= now_gt + 1e-9:
        t += step
        result.steps += 1
        result.happenings += _step(db, timeline, community_key, location, members, ts, t, step)
        if budget_ms is not None and (time.perf_counter() - started) * 1000 > budget_ms:
            break
    _tick(db, timeline, community_key, location, t)
    result.ms = (time.perf_counter() - started) * 1000
    return result


def _tick(db, timeline: int, community: str, location: str | None, gt: float) -> None:
    _event(db, timeline, TICK_TYPE, location, [], {"community": community}, gt)


def _bootstrap(db, timeline: int, members: list[int], location: str | None, gt: float) -> None:
    """Seeded spouses start out married, with the closeness that goes with years together."""
    for a in members:
        for b in members:
            if a >= b or bond(db, a, b):
                continue
            r1, r2 = rel(db, a, b), rel(db, b, a)
            if r1.get("kinship") == "spouse" and r2.get("kinship") == "spouse":
                lo, hi = a, b
                db.execute("INSERT INTO romance_bond_change (timeline_id, a_id, b_id, stage_from, stage_to, exclusive,"
                           " secret, reason, game_time) VALUES (?,?,?,NULL,'married',1,0,'seed',?)", (timeline, lo, hi, gt))
                for x, y in ((a, b), (b, a)):
                    _delta(db, timeline, x, y, {"intimacy": 0.6, "commitment": 0.75, "satisfaction": 0.3},
                           "seed: married for years", gt)


def _step(db, timeline: int, community: str, location: str | None, members: list[int], ts: dict, t: float,
          step: float) -> int:
    rng = _rng("romance", timeline, community, f"{t:.5f}")
    happened = 0
    new_day = int(t) > int(t - step)
    hour = (t % 1.0) * 24
    night = hour >= 20 or hour < 4
    evening = 16 <= hour < 22
    if new_day:
        happened += _daily(db, timeline, location, members, ts, t, rng)
    happened += _flirting(db, timeline, location, members, ts, t, step, rng)
    for b in db.execute(f"SELECT * FROM v_romance_bond WHERE stage IN ({_marks(ACTIVE)}) ORDER BY a_id, b_id",
                        ACTIVE).fetchall():
        if b["a_id"] not in ts or b["b_id"] not in ts:
            continue
        happened += _together(db, timeline, location, b, ts, t, step, rng, night, evening)
        happened += _stage_up(db, timeline, location, b, ts, t, step, rng)
    happened += _asking(db, timeline, location, members, ts, t, step, rng)
    return happened


def _recent_change(db, npc: int, since: float) -> bool:
    return db.execute("SELECT 1 FROM romance_bond_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
                      " AND c.game_time <= s.cutoff_gt WHERE (c.a_id = ? OR c.b_id = ?) AND c.game_time > ?"
                      " AND c.reason <> 'seed' LIMIT 1", (npc, npc, since)).fetchone() is not None


def _daily(db, timeline, location, members, ts, t, rng) -> int:
    happened = 0
    day = int(t)
    # attraction drifts toward each pair's spark, a few pairs a day
    for a in members:
        for b in members:
            if a == b or (day + a * 7 + b * 13) % 5:
                continue
            r = rel(db, a, b)
            if not r or (r["kinship"] and r["kinship"] != "spouse"):
                continue
            target = max(_spark(a, b, ts[a], ts[b]), r["base_attraction"] or 0.0) * min(1.0, r["familiarity"] + 0.3)
            gap = target - r["attraction"]
            if abs(gap) >= 0.05:
                _delta(db, timeline, a, b, {"attraction": 0.25 * gap}, "attraction drifts", t)
    # how a bond feels drifts back toward ordinary contentment unless something keeps it down
    for b in db.execute(f"SELECT * FROM v_romance_bond WHERE stage IN ({_marks(ACTIVE)})", ACTIVE).fetchall():
        for me, them in ((b["a_id"], b["b_id"]), (b["b_id"], b["a_id"])):
            gap = SATISFACTION_BASE - rel(db, me, them).get("satisfaction", 0.0)
            if abs(gap) >= 0.02:
                _delta(db, timeline, me, them, {"satisfaction": 0.05 * gap}, "everyday life", t)
    # jealousy fades
    for row in db.execute(f"SELECT from_id, to_id, jealousy FROM v_relationship WHERE jealousy > 0.01"
                          f" AND from_id IN ({_marks(members)})", members).fetchall():
        _delta(db, timeline, row[0], row[1], {"jealousy": -min(row[2], JEALOUSY_FADE)}, "jealousy fades", t)
    # breakups
    for b in db.execute(f"SELECT * FROM v_romance_bond WHERE stage IN ({_marks(ACTIVE)}) ORDER BY a_id, b_id",
                        ACTIVE).fetchall():
        x, y = b["a_id"], b["b_id"]
        if x not in ts or y not in ts:
            continue
        for me, them in ((x, y), (y, x)):
            r = rel(db, me, them)
            unhappy = r.get("satisfaction", 0) < -0.3
            betrayed = r.get("jealousy", 0) >= 0.45 and r.get("trust", 0) < 0.3
            if b["stage"] == "affair":
                per_day = 0.03 + (0.1 if unhappy else 0.0)
            elif unhappy or betrayed:
                per_day = 0.08 * (1.2 - ts[me].get("loyal", 0.5)) * (1.2 - ts[me].get("trusting", 0.5))
                if b["stage"] == "married":
                    per_day *= 0.1 if betrayed else 0.03   # a marriage rarely ends, and almost never over a bad patch
            else:
                continue
            if rng.random() < _chance(per_day, 1.0):
                happened += 1
                _break_up(db, timeline, location, me, them, b, t, "betrayed" if betrayed else "unhappy")
                break
    # reconciliation, after a while, only when there is closeness left on both sides
    for b in db.execute("SELECT * FROM v_romance_bond WHERE stage = 'ended' AND since_gt < ? AND since_gt > ?"
                        " ORDER BY a_id, b_id", (t - RECONCILE_AFTER, t - 120)).fetchall():
        x, y = b["a_id"], b["b_id"]
        if x not in ts or y not in ts or active_bonds(db, x) or active_bonds(db, y):
            continue
        rx, ry = rel(db, x, y), rel(db, y, x)
        if min(rx.get("intimacy", 0), ry.get("intimacy", 0)) >= 0.3 and \
                min(rx.get("satisfaction", 0), ry.get("satisfaction", 0)) > -0.5 and rng.random() < 0.02:
            happened += 1
            event_id = set_bond(db, timeline, x, y, "dating", "reconciled", t, location)
            for me, them in ((x, y), (y, x)):
                _delta(db, timeline, me, them, {"jealousy": -rel(db, me, them).get("jealousy", 0), "satisfaction": 0.15},
                       "reconciled", t, event_id)
                _remember(db, timeline, me, f"{_name(db, them)} and I talked it through and decided to try again.",
                          [them], t, 0.6, 0.4, event_id, location)
    return happened


def _spark(a: int, b: int, ta: dict, tb: dict) -> float:
    if not attracted(ta, tb):
        return 0.0
    rng = _rng("spark", a, b)
    chance = 0.2 + 0.4 * ta.get("flirtatious", 0.3)
    return round(rng.uniform(0.3, 0.9), 3) if rng.random() < chance else 0.0


def _break_up(db, timeline, location, me, them, b, t, why) -> None:
    secret = bool(b["secret"])
    event_id = set_bond(db, timeline, me, them, "ended", f"{_name(db, me)} ended it: {why}", t, location, secret=secret)
    for x, y in ((me, them), (them, me)):
        r = rel(db, x, y)
        _delta(db, timeline, x, y, {"commitment": -0.5 * r.get("commitment", 0), "satisfaction": -0.1},
               "the bond ended", t, event_id)
    _remember(db, timeline, me, f"I ended things with {_name(db, them)}.", [them], t, 0.7, -0.3, event_id, location,
              "I couldn't go on the way things were." if why == "unhappy" else "I couldn't trust them any more.")
    _remember(db, timeline, them, f"{_name(db, me)} ended things with me.", [me], t, 0.75, -0.6, event_id, location)


def _flirting(db, timeline, location, members, ts, t, step, rng) -> int:
    """People who are taken flirting with someone else, in public. Someone close may see it."""
    happened = 0
    for a in members:
        partners = [partner_of(b, a) for b in active_bonds(db, a) if b["stage"] != "affair"]
        if not partners:
            continue
        ta = ts[a]
        per_day = FLIRT_PER_DAY * ta["flirtatious"] * (1 - 0.5 * ta["monogamy"]) * (0.5 + ta.get("curious", 0.5))
        if rng.random() >= _chance(per_day, step):
            continue
        targets = [m for m in members if m != a and m not in partners and attracted(ta, ts[m])
                   and not (rel(db, a, m).get("kinship"))]
        if not targets:
            continue
        b = rng.choice(targets)
        answer = consent.decide(db, b, a, "flirt", "invite", ts[b], None, t, timeline=timeline,
                                jitter=rng.uniform(-0.05, 0.05), log=False)
        witnesses = {a: "participated", b: "participated"}
        for p in partners:
            if rng.random() < 0.35:
                witnesses[p] = "saw"
        for m in members:
            if m not in witnesses and rng.random() < 0.1:
                witnesses[m] = "saw"
        event_id = _event(db, timeline, "romance.flirt", location, [a, b], {"returned": answer.yes}, t, witnesses)
        db.execute("INSERT INTO encounter (timeline_id, a_id, b_id, kind, outcome, event_id, game_time)"
                   " VALUES (?,?,?, 'flirt', ?, ?, ?)", (timeline, a, b, "completed" if answer.yes else "declined",
                                                          event_id, t))
        happened += 1
    return happened


def _together(db, timeline, location, b, ts, t, step, rng, night, evening) -> int:
    x, y = b["a_id"], b["b_id"]
    if night and db.execute("SELECT 1 FROM v_encounter WHERE a_id IN (?, ?) AND b_id IN (?, ?) AND kind = 'intimate'"
                            " AND game_time > ? LIMIT 1", (x, y, x, y, t - 0.75)).fetchone() is None:
        per_day = INTIMATE_PER_DAY * (ts[x]["drive"] + ts[y]["drive"])
        if b["stage"] == "affair":
            per_day *= 0.5
        if rng.random() < _chance(per_day, 1.0) * min(1.0, step / STEP_DAYS) / 2:   # two night steps a day
            asker, other = (x, y) if ts[x]["drive"] + rng.uniform(-0.2, 0.2) >= ts[y]["drive"] else (y, x)
            _intimate(db, timeline, location, asker, other, b, ts, t, rng)
            return 1
    if evening and b["stage"] != "affair" and rng.random() < _chance(DATE_PER_DAY, step):
        asker, other = (x, y) if rng.random() < 0.5 else (y, x)
        _date(db, timeline, location, asker, other, b, ts, t, rng)
        return 1
    return 0


def _date(db, timeline, location, asker, other, b, ts, t, rng) -> None:
    place, _ = best_place(db, location, asker, other, "date", t)
    enc = db.execute("INSERT INTO encounter (timeline_id, a_id, b_id, kind, place_key, outcome, game_time)"
                     " VALUES (?,?,?, 'date', ?, 'cancelled', ?)",
                     (timeline, asker, other, place["place_key"] if place else None, t)).lastrowid
    answers = [consent.decide(db, p, q, "date", "invite", ts[p], b["stage"], t, encounter_id=enc, timeline=timeline,
                              jitter=rng.uniform(-0.05, 0.05)) for p, q in ((asker, other), (other, asker))]
    if not all(a.yes for a in answers):
        db.execute("UPDATE encounter SET outcome = 'declined' WHERE encounter_id = ?", (enc,))
        if answers[0].yes:
            _delta(db, timeline, asker, other, {"satisfaction": -0.01}, "they didn't want to spend the evening", t)
        return
    db.execute("UPDATE encounter SET outcome = 'completed' WHERE encounter_id = ?", (enc,))
    for p, q in ((asker, other), (other, asker)):
        _delta(db, timeline, p, q, {"intimacy": 0.03, "satisfaction": 0.03}, "an evening together", t)


def _intimate(db, timeline, location, asker, other, b, ts, t, rng) -> None:
    place, privacy = best_place(db, location, asker, other, "intimate", t)
    enc = db.execute("INSERT INTO encounter (timeline_id, a_id, b_id, kind, place_key, outcome, game_time)"
                     " VALUES (?,?,?, 'intimate', ?, 'cancelled', ?)",
                     (timeline, asker, other, place["place_key"] if place else None, t)).lastrowid
    stage = b["stage"]

    def ask(phase):
        return [consent.decide(db, p, q, "intimate", phase, ts[p], stage, t, place_privacy=privacy, encounter_id=enc,
                               timeline=timeline, jitter=rng.uniform(-0.05, 0.05))
                for p, q in ((asker, other), (other, asker))]

    invite = ask("invite")
    if not all(a.yes for a in invite):
        db.execute("UPDATE encounter SET outcome = 'declined' WHERE encounter_id = ?", (enc,))
        if invite[0].yes:   # the other said no: a small disappointment for the asker, nothing for the one who said no
            _delta(db, timeline, asker, other, {"satisfaction": -0.01}, "not tonight", t)
        return
    if place is None or privacy < PRIVATE_ENOUGH:
        return              # nowhere private: stays 'cancelled'
    for phase in ("arrival", "start"):
        if not all(a.yes for a in ask(phase)):
            db.execute("UPDATE encounter SET outcome = 'withdrawn' WHERE encounter_id = ?", (enc,))
            return
    secret = bool(b["secret"])
    noticed = _noticers(db, place, asker, other, rng, [])
    interrupted = rng.random() < (1 - privacy) * 0.3
    etype = "romance.interrupted" if interrupted else "romance.encounter"
    event_id = _event(db, timeline, etype, location, [asker, other],
                      {"kind": "intimate", "place": place["place_key"], "place_name": place["name"],
                       "mode": "offscreen", "secret": secret},
                      t, {asker: "participated", other: "participated", **noticed})
    db.execute("UPDATE encounter SET outcome = ?, event_id = ? WHERE encounter_id = ?",
               ("interrupted" if interrupted else "completed", event_id, enc))
    bonded = stage in ("dating", "partners", "betrothed", "married")
    for p, q in ((asker, other), (other, asker)):
        if interrupted:
            _delta(db, timeline, p, q, {"satisfaction": -0.03}, "interrupted", t, event_id)
            _remember(db, timeline, p, f"{_name(db, q)} and I were interrupted at {place['name']}.", [q], t, 0.6, -0.3,
                      event_id, location)
            continue
        deltas = {"satisfaction": 0.04}
        if bonded:
            deltas.update(intimacy=0.02, commitment=0.01)
        elif stage == "affair":
            deltas.update(intimacy=0.02)
        _delta(db, timeline, p, q, deltas, "a night together", t, event_id)
        note = " Nobody can know." if secret else ""
        _remember(db, timeline, p, f"I spent the night with {_name(db, q)} at {place['name']}.{note}", [q], t,
                  0.55 if not secret else 0.7, 0.5, event_id, location)


def _stage_up(db, timeline, location, b, ts, t, step, rng) -> int:
    stage = b["stage"]
    nxt = NEXT_STAGE.get(stage)
    if not nxt or t - b["since_gt"] < MIN_DAYS_AT[stage] or rng.random() >= _chance(STAGE_UP_PER_DAY, step):
        return 0
    x, y = b["a_id"], b["b_id"]
    if _recent_change(db, x, t - PERSON_STAGE_GAP) or _recent_change(db, y, t - PERSON_STAGE_GAP):
        return 0
    need_intimacy, need_commitment = NEEDS[nxt]
    for p, q in ((x, y), (y, x)):
        r = rel(db, p, q)
        if r.get("satisfaction", 0) < 0.1 or r.get("intimacy", 0) < need_intimacy or r.get("commitment", 0) < need_commitment:
            return 0
        if nxt in ("partners", "betrothed", "married") and any(partner_of(o, p) != q for o in active_bonds(db, p)):
            return 0        # still seeing someone else
    event_id = set_bond(db, timeline, x, y, nxt, "both wanted more", t, location)
    for p, q in ((x, y), (y, x)):
        _delta(db, timeline, p, q, {"commitment": 0.15}, f"now {nxt}", t, event_id)
        words = {"dating": f"{_name(db, q)} and I are seeing each other properly now.",
                 "partners": f"{_name(db, q)} and I agreed to be together, just the two of us.",
                 "betrothed": f"{_name(db, q)} and I are to be married.",
                 "married": f"{_name(db, q)} and I were married."}[nxt]
        _remember(db, timeline, p, words, [q], t, 0.8, 0.7, event_id, location)
    return 1


def _asking(db, timeline, location, members, ts, t, step, rng) -> int:
    """Someone with strong private interest asks: to court, or (rarely) for an affair."""
    recent = db.execute(
        f"SELECT 1 FROM romance_bond_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
        f" AND c.game_time <= s.cutoff_gt WHERE c.stage_to IN ('courting','affair') AND c.reason <> 'seed'"
        f" AND c.game_time > ? AND c.a_id IN ({_marks(members)}) LIMIT 1", (t - COMMUNITY_NEW_BOND_GAP, *members)).fetchone()
    if recent:
        return 0
    for a in members:
        if _recent_change(db, a, t - PERSON_STAGE_GAP):
            continue
        taken = any(o["stage"] != "affair" for o in active_bonds(db, a))
        for b in members:
            if b == a or bond(db, a, b):
                continue    # already together, or exes (they only come back through reconciliation)
            r = rel(db, a, b)
            if not r or (r["kinship"] and r["kinship"] != "spouse"):
                continue
            want = interest(db, a, b, ts[a], ts[b])
            threshold, per_day = (AFFAIR_INTEREST, AFFAIR_PER_DAY) if taken else (ASK_INTEREST, ASK_PER_DAY)
            if want < threshold or _cooling_down(db, a, b, t):
                continue
            if rng.random() >= _chance(per_day * (1 - 0.6 * ts[a]["shy"]), step):
                continue
            _ask(db, timeline, location, a, b, ts, t, taken or any(o["stage"] != "affair" for o in active_bonds(db, b)))
            return 1
    return 0


def _cooling_down(db, a: int, b: int, t: float) -> bool:
    rows = db.execute("SELECT game_time FROM v_visible_event WHERE type = 'romance.declined' AND"
                      " json_extract(actors_json, '$[0]') = ? AND json_extract(actors_json, '$[1]') = ?"
                      " AND game_time > ? ORDER BY game_time DESC", (a, b, t - 60)).fetchall()
    if not rows:
        return False
    if len(rows) >= 2:
        return t - rows[0][0] < GIVE_UP_DAYS + 30   # two nos: they stop asking for a long while
    return t - rows[0][0] < DECLINE_COOLDOWN


def _ask(db, timeline, location, a, b, ts, t, affair) -> None:
    tb = ts[b]
    their = interest(db, b, a, tb, ts[a])
    if affair:
        their *= (1 - 0.5 * tb["monogamy"])
    yes = their >= (AFFAIR_INTEREST if affair else ACCEPT_INTEREST)
    stage = "affair" if affair else "courting"
    if not yes:
        event_id = _event(db, timeline, "romance.declined", location, [a, b], {"asked_for": stage}, t,
                          {a: "participated", b: "participated"})
        _delta(db, timeline, a, b, {"attraction": -0.05, "satisfaction": -0.02}, "asked and was turned down", t, event_id)
        _remember(db, timeline, a, f"I told {_name(db, b)} how I feel. They turned me down.", [b], t, 0.6, -0.4,
                  event_id, location)
        _remember(db, timeline, b, f"{_name(db, a)} told me how they feel about me. I said no.", [a], t, 0.5, -0.1,
                  event_id, location)
        return
    event_id = set_bond(db, timeline, a, b, stage, "asked" if not affair else "affair", t, location, secret=affair)
    for p, q in ((a, b), (b, a)):
        _delta(db, timeline, p, q, {"intimacy": 0.05, "satisfaction": 0.1}, f"started {stage}", t, event_id)
    if affair:
        for p, q in ((a, b), (b, a)):
            _remember(db, timeline, p, f"{_name(db, q)} and I have started seeing each other in secret.", [q], t, 0.8,
                      0.3, event_id, location, "Nobody can find out.")
    else:
        _remember(db, timeline, a, f"I asked {_name(db, b)} if I could court them, and they said yes.", [b], t, 0.75,
                  0.7, event_id, location)
        _remember(db, timeline, b, f"{_name(db, a)} asked to court me, and I said yes.", [a], t, 0.75, 0.6,
                  event_id, location)


# ---- in conversation ----

STAGE_WORDS = {"courting": "{other} and I are courting", "dating": "{other} and I are seeing each other",
               "partners": "{other} and I are together", "betrothed": "{other} and I are betrothed",
               "married": "{other} is my spouse"}


def recall_lines(db, npc: int, now_gt: float) -> list[str]:
    """npc's own romantic life, in their words, for their prompt only."""
    from livingworld.memory.recall import time_ago
    lines = []
    for b in active_bonds(db, npc):
        other = partner_of(b, npc)
        name = _name(db, other)
        if b["stage"] == "affair":
            lines.append(f"I've been secretly seeing {name} for {time_ago(now_gt - b['since_gt']).replace(' ago', '')}."
                         " Nobody is meant to know; I would only ever hint at it to someone I trust completely.")
            continue
        line = STAGE_WORDS[b["stage"]].format(other=name)
        r = rel(db, npc, other)
        if r.get("jealousy", 0) >= 0.3:
            line += f", but I've been jealous lately: {name}'s eye wanders"
        elif r.get("satisfaction", 0) <= -0.3:
            line += ", but things have not been good between us"
        lines.append(line + ".")
    for b in db.execute("SELECT * FROM v_romance_bond WHERE (a_id = ? OR b_id = ?) AND stage = 'ended' AND since_gt > ?",
                        (npc, npc, now_gt - 30)).fetchall():
        lines.append(f"{_name(db, partner_of(b, npc))} and I split up {time_ago(now_gt - b['since_gt'])}.")
    return lines
