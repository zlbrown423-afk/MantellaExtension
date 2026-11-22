"""The monthly reckoning: rules only, no LLM. One run per 30-day period of a charter, caught up lazily.

Order within one period (design doc, "The tick, in order"):

1. compliance  - judge each term (personnel cap, patrols, report); unwatched terms are excused
2. money       - stipend and equipment allowance from the court (full, half or none), wages and feed out
3. measures    - the seven influence measures, power, legitimacy and the gap between them
4. stances     - each official's view from their weights, traits and the measures
5. one move    - at most one official acts (suspend, restore, urge a review, withhold, commend)
6. the Jarl's  - missed terms cost his trust; a clean month earns a little respect
   feelings

Everything is written on the active save timeline. Periods are numbered from the charter's signing; the last
period done is read back from ``v_compliance``, so loading an older save simply redoes later periods.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from livingworld.civic import institutions, ledger
from livingworld.lordship import COMPANY, COURT
from livingworld.lordship import charter as C
from livingworld.lordship import data as D
from livingworld.social import traits as T

MAX_CATCH_UP = 24          # periods per call; a longer gap is finished on the next conversation
WINDOW = 3                 # periods the compliance record looks back over
BANDS = ((0.0, "none"), (0.2, "small"), (0.45, "medium"), (0.8, "large"))   # gap below the number -> band
POSITIONS = ((0.5, "champion"), (0.15, "support"), (-0.15, "wary"), (-0.5, "oppose"))
SCORE_STEP = 0.05          # a stance row is written when the score moves this much (or the position changes)
OUTCOME_VALUE = {"met": 1.0, "short": 0.5, "missed": 0.0}


def band_of(gap: float) -> str:
    for limit, name in BANDS:
        if gap <= limit:
            return name
    return "extreme"


def position_of(score: float) -> str:
    for limit, name in POSITIONS:
        if score >= limit:
            return name
    return "hostile"


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class Snapshot:
    period: int
    riders: int
    horses: int
    cap: int
    treasury: int
    jarl: float | None              # Balgruuf's regard of the player, -1..1 (None: never met)
    court: float | None             # the other officials' mean regard
    approval: float | None          # district (or settlements') reputation 'regard', -1..1
    loyalty: dict[str, float]       # per district community
    compliance: float               # 0..1 over the last WINDOW periods (1.0 with nothing judged)
    security: float | None          # L3; None = not yet watched
    power: float
    legitimacy: float
    gap: float
    band: str
    stipend_factor: float = 1.0
    missed: list[str] = field(default_factory=list)
    short: list[str] = field(default_factory=list)

    def measures(self) -> dict:
        return {"military_strength": {"riders": self.riders, "horses": self.horses, "cap": self.cap},
                "treasury": self.treasury, "public_approval": self.approval, "settlement_loyalty": self.loyalty,
                "noble_relationships": self.court, "jarl_relationship": self.jarl,
                "territorial_security": self.security, "compliance": round(self.compliance, 3),
                "stipend_factor": self.stipend_factor, "missed": self.missed, "short": self.short}


@dataclass
class Result:
    periods: int = 0
    paid: int = 0
    moves: list[str] = field(default_factory=list)
    status: str | None = None


# ---- helpers ----

def regard(db, from_id: int | None, to_id: int | None) -> float | None:
    if not from_id or not to_id:
        return None
    r = db.execute("SELECT trust, affinity, respect FROM v_relationship WHERE from_id = ? AND to_id = ?",
                   (from_id, to_id)).fetchone()
    return None if r is None else round((r[0] + r[1] + r[2]) / 3.0, 4)


def _community_regard(db, keys: tuple[str, ...] | list[str], player_id: int) -> dict[str, float]:
    out = {}
    for key in keys:
        r = db.execute("SELECT r.value FROM v_community_reputation r JOIN community c ON c.community_id = r.community_id"
                       " WHERE c.key = ? AND r.subject_id = ? AND r.domain = 'regard'", (key, player_id)).fetchone()
        if r is not None and r[0] is not None:
            out[key] = round(r[0], 4)
    return out


def approval(db, charter: C.Charter, player_id: int) -> tuple[float | None, dict[str, float]]:
    """District communities' view (loyalty), and public approval: the district's mean, else every settlement's."""
    district = D.district(charter.district_key)
    loyalty = _community_regard(db, district.communities if district else (), player_id)
    if loyalty:
        return round(sum(loyalty.values()) / len(loyalty), 4), loyalty
    keys = [k for (k,) in db.execute("SELECT key FROM community WHERE kind = 'settlement'").fetchall()]
    towns = _community_regard(db, keys, player_id)
    return (round(sum(towns.values()) / len(towns), 4) if towns else None), {}


def periods_done(db, charter: C.Charter) -> int:
    r = db.execute("SELECT MAX(period) FROM v_compliance WHERE charter_id = ?", (charter.charter_id,)).fetchone()
    return int(r[0] or 0)


def _bounds(charter: C.Charter, period: int) -> tuple[float, float]:
    days = D.terms().period_days
    return charter.signed_gt + (period - 1) * days, charter.signed_gt + period * days


# ---- 1. compliance ----

def judge(db, charter: C.Charter, period: int) -> list[tuple[str, float | None, float | None, str, str]]:
    """(term, required, delivered, outcome, note) for each term in this period."""
    start, end = _bounds(charter, period)
    t = charter.terms
    out = []
    riders, _ = C.strength(db, end)
    cap = int(t.get("personnel_cap") or 0)
    if not C.watched(db, "strength", charter, end):
        out.append(("personnel", cap, riders, "excused", "strength not yet reported by the game"))
    elif riders <= cap:
        out.append(("personnel", cap, riders, "met", f"{riders} riders within the cap of {cap}"))
    elif riders <= cap * 1.25:
        out.append(("personnel", cap, riders, "short", f"{riders} riders, a little over the cap of {cap}"))
    else:
        out.append(("personnel", cap, riders, "missed", f"{riders} riders, well over the cap of {cap}"))
    per_week = float(t.get("patrols_per_week") or 0)
    required = round(per_week * D.terms().period_days / 7.0)
    if required <= 0:
        pass
    elif not C.watched(db, "patrol", charter, end):
        out.append(("patrols", required, None, "excused", "patrols are not yet counted in game"))
    else:
        done = sum(int(f.get("count", 1)) for f in C.facts(db, "patrol", start, end))
        outcome = "met" if done >= required else "short" if done * 2 >= required else "missed"
        out.append(("patrols", required, done, outcome, f"{done} of {required} patrols ridden"))
    every = int(t.get("report_every_days") or 0)
    days = D.terms().period_days
    due = every > 0 and (period * days) % every < days     # a report falls due within this period
    if due:
        since = end - max(every, days)
        if not C.watched(db, "report", charter, end):
            out.append(("report", 1, None, "excused", "reports are not yet filed in game"))
        else:
            filed = len(C.facts(db, "report", since, end))
            out.append(("report", 1, filed, "met" if filed else "missed",
                        "report filed with the steward" if filed else "no report filed with the steward"))
    return out


def compliance_score(db, charter: C.Charter, upto_period: int) -> float:
    rows = db.execute("SELECT outcome FROM v_compliance WHERE charter_id = ? AND period > ? AND period <= ?"
                      " AND outcome <> 'excused'", (charter.charter_id, upto_period - WINDOW, upto_period)).fetchall()
    if not rows:
        return 1.0
    return sum(OUTCOME_VALUE[o] for (o,) in rows) / len(rows)


# ---- 2. money ----

def _withheld(db, charter: C.Charter, period: int) -> bool:
    """Proventus said last period he would hold back the next payment."""
    return db.execute("SELECT 1 FROM v_official_stance WHERE subject = ? AND move = 'withhold' AND period = ?"
                      " AND game_time >= ? LIMIT 1", (charter.holder, period - 1, charter.signed_gt)).fetchone() is not None


def settle(db, charter: C.Charter, period: int, outcomes: list[tuple], status: str | None, eid: int) -> tuple[int, float]:
    """Pay the stipend and allowance, take wages and feed. Returns (paid in, stipend factor)."""
    _, end = _bounds(charter, period)
    judged = [o[3] for o in outcomes if o[3] != "excused"]
    factor = 0.0 if "missed" in judged else 0.5 if "short" in judged else 1.0
    if status == "suspended" or _withheld(db, charter, period):
        factor = 0.0
    t = charter.terms
    company, court = ledger.purse_of(COMPANY), ledger.purse_of(COURT)
    stipend = int(round(float(t.get("stipend_per_year") or 0) / 12.0 * factor))
    allowance = int(round(float(t.get("equipment_per_year") or 0) / 12.0 * factor))
    memo = f"charter v{charter.version}, month {period}"
    ledger.transfer(db, court, company, stipend, "stipend", end, memo, eid)
    ledger.transfer(db, court, company, allowance, "allowance", end, memo, eid)
    riders, horses = C.strength(db, end)
    tt = D.terms()
    wages = max(0, riders - 1) * tt.wage_per_rider_month          # the founder draws no wage
    feed = max(0, horses) * tt.feed_per_horse_month
    ledger.record(db, company, -wages, "wage", end, "the riders", memo, eid)
    ledger.record(db, company, -feed, "upkeep", end, "feed and farrier", memo, eid)
    return stipend + allowance, factor


# ---- 3. measures ----

def measure(db, charter: C.Charter, period: int, player_id: int, factor: float = 1.0,
            outcomes: list[tuple] | None = None) -> Snapshot:
    _, end = _bounds(charter, period)
    t = charter.terms
    riders, horses = C.strength(db, end)
    cap = max(1, int(t.get("personnel_cap") or 1))
    money = C.treasury(db, end)
    jarl_off = D.official("balgruuf")
    jarl = regard(db, C.npc_for(db, jarl_off), player_id) if jarl_off else None
    others = [regard(db, C.npc_for(db, o), player_id) for o in D.officials() if o.key != "balgruuf"]
    others = [x for x in others if x is not None]
    court = round(sum(others) / len(others), 4) if others else None
    appr, loyalty = approval(db, charter, player_id)
    comp = compliance_score(db, charter, period)
    strength_ratio = min(3.0, riders / cap)
    yearly = max(1200.0, float(t.get("stipend_per_year") or 0))
    treasury_ratio = _clamp(money / yearly, 0.0, 2.0)
    power = round(0.75 * strength_ratio + 0.25 * treasury_ratio, 4)

    def as01(x: float | None) -> float:
        return 0.5 if x is None else (x + 1.0) / 2.0
    support = 0.4 * as01(jarl) + 0.3 * comp + 0.15 * as01(appr) + 0.15 * as01(court)
    legitimacy = round(float(t.get("expected_power") or 1.0) * (0.5 + support), 4)
    gap = round(power - legitimacy, 4)
    outcomes = outcomes or []
    return Snapshot(period, riders, horses, cap, money, jarl, court, appr, loyalty, comp, None, power, legitimacy,
                    gap, band_of(gap), factor, [o[0] for o in outcomes if o[3] == "missed"],
                    [o[0] for o in outcomes if o[3] == "short"])


# ---- 4. stances ----

@dataclass
class Stance:
    official: D.Official
    npc_id: int | None
    score: float
    position: str
    reasons: list[str]


def _reason(kind: str, value: float, snap: Snapshot) -> str:
    if kind == "compliance":
        if value >= 0.5:
            return "the Wardens have kept the terms of their charter"
        missed = ", ".join(snap.missed + snap.short) or "some of their terms"
        return f"the Wardens have fallen short of their charter ({missed})"
    if kind == "restraint":
        if value >= 0:
            return "the Wardens stay well within what their charter allows"
        return (f"the Wardens have grown stronger than their charter allows ({snap.riders} riders against a cap of "
                f"{snap.cap}, without the backing to match)")
    if kind == "thrift":
        if value >= 0:
            return "the Wardens cost the court no more than was agreed and pay their way"
        return f"the Wardens' purse is in debt ({snap.treasury} gold) and their riders go unpaid"
    if kind == "standing":
        return ("the Jarl thinks well of the Wardens' founder" if value >= 0
                else "the Jarl has cooled towards the Wardens' founder")
    return "they personally think well of the founder" if value >= 0 else "they personally distrust the founder"


def stance(db, official: D.Official, snap: Snapshot, player_id: int) -> Stance:
    npc_id = C.npc_for(db, official)
    own = regard(db, npc_id, player_id)
    factors = {
        "compliance": 2.0 * snap.compliance - 1.0,
        "restraint": _clamp(-snap.gap * 1.25, -1.0, 0.4),
        "thrift": 0.3 if snap.treasury >= 0 else -min(1.0, -snap.treasury / 200.0),
        "standing": snap.jarl or 0.0,
        "regard": own or 0.0,
    }
    weights = dict(official.weights)
    tr = T.traits_of(db, npc_id) if npc_id else {}
    if "restraint" in weights:
        weights["restraint"] *= 1.5 - float(tr.get("trusting", 0.5))
    if "thrift" in weights:
        weights["thrift"] *= 0.5 + float(tr.get("greedy", 0.5))
    total = sum(abs(w) for w in weights.values()) or 1.0
    score = round(_clamp(sum(w * factors.get(k, 0.0) for k, w in weights.items()) / total), 4)
    ranked = sorted(((abs(w * factors.get(k, 0.0)), k) for k, w in weights.items() if w), reverse=True)
    reasons = [_reason(k, factors[k], snap) for weight, k in ranked[:2] if weight >= 0.02]
    return Stance(official, npc_id, score, position_of(score), reasons)


def _last_stance(db, npc_id: int, subject: str):
    return db.execute("SELECT position, score FROM v_official_stance WHERE npc_id = ? AND subject = ?"
                      " ORDER BY game_time DESC, stance_id DESC LIMIT 1", (npc_id, subject)).fetchone()


def write_stance(db, s: Stance, subject: str, period: int | None, gt: float, move: str | None = None,
                 force: bool = False) -> bool:
    if s.npc_id is None:
        return False
    last = _last_stance(db, s.npc_id, subject)
    if not force and move is None and last is not None and last[0] == s.position and abs(last[1] - s.score) < SCORE_STEP:
        return False
    db.execute("INSERT INTO official_stance (timeline_id, npc_id, official_key, subject, position, score, reasons_json,"
               " move, period, game_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (institutions.active_timeline(db), s.npc_id, s.official.key, subject, s.position, s.score,
                json.dumps(s.reasons), move, period, gt))
    return True


# ---- 5. one move ----

MOVE_ORDER = ("suspend", "restore", "urge_review", "confirm", "withhold", "commend")


def choose_move(charter: C.Charter, status: str | None, snap: Snapshot, stances: list[Stance],
                clean_streak: int) -> tuple[Stance, str] | None:
    by = {s.official.key: s for s in stances if s.npc_id is not None}
    options: list[tuple[str, Stance]] = []
    jarl, steward, housecarl = by.get("balgruuf"), by.get("proventus"), by.get("irileth")
    hostile = ("oppose", "hostile")
    friendly = ("support", "champion")
    if jarl and status != "suspended" and snap.band == "extreme" and jarl.position in hostile:
        options.append(("suspend", jarl))
    if jarl and status == "suspended" and snap.band in ("none", "small") and jarl.position in friendly:
        options.append(("restore", jarl))
    if housecarl and status == "active" and snap.band in ("medium", "large", "extreme") and housecarl.position in hostile:
        options.append(("urge_review", housecarl))
    if jarl and status == "under_review" and snap.band in ("none", "small") and jarl.position in friendly:
        options.append(("confirm", jarl))
    if steward and status != "suspended" and steward.position in hostile and (
            "report" in snap.missed or snap.treasury < 0 or snap.missed):
        options.append(("withhold", steward))
    if jarl and status == "active" and snap.band == "none" and jarl.position == "champion" and clean_streak >= 3 \
            and clean_streak % 3 == 0:
        options.append(("commend", jarl))
    if not options:
        return None
    move, who = min(options, key=lambda o: MOVE_ORDER.index(o[0]))
    return who, move


MOVE_WORDS = {
    "suspend": "{name} suspended the Plains Wardens' charter: their strength has outgrown their standing",
    "restore": "{name} restored the Plains Wardens' charter",
    "urge_review": "{name} urged the Jarl to review the Plains Wardens' charter",
    "confirm": "{name} confirmed the Plains Wardens' charter after review",
    "withhold": "{name} held back the Plains Wardens' next payment",
    "commend": "{name} commended the Plains Wardens' service before the court",
}


def apply_move(db, charter: C.Charter, who: Stance, move: str, period: int, gt: float, player_id: int) -> str:
    text = MOVE_WORDS[move].format(name=who.official.name)
    actors = [a for a in (who.npc_id, player_id) if a]
    eid = C.event(db, "lordship.move", gt, actors, {"move": move, "by": who.official.key, "charter_id": charter.charter_id,
                                                     "period": period, "text": text}, location="Dragonsreach")
    if move == "suspend":
        C.set_status(db, charter.charter_id, "suspended", text, gt, eid)
    elif move in ("restore", "confirm"):
        C.set_status(db, charter.charter_id, "active", text, gt, eid)
    elif move == "urge_review":
        C.set_status(db, charter.charter_id, "under_review", text, gt, eid)
    elif move == "commend" and who.npc_id:
        _feel(db, who.npc_id, player_id, {"respect": 0.03, "trust": 0.02}, text, eid, gt)
    write_stance(db, who, charter.holder, period, gt, move=move)
    return text


# ---- 6. the Jarl's feelings ----

def _feel(db, from_id: int, to_id: int, deltas: dict, reason: str, eid: int | None, gt: float) -> None:
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
               (from_id, to_id, "acquaintance", gt))
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)",
               (institutions.active_timeline(db), from_id, to_id, json.dumps(deltas), eid, reason, gt))


def _clean_streak(db, charter: C.Charter, period: int) -> int:
    streak = 0
    for p in range(period, 0, -1):
        rows = [o for (o,) in db.execute("SELECT outcome FROM v_compliance WHERE charter_id = ? AND period = ?",
                                         (charter.charter_id, p)).fetchall()]
        judged = [o for o in rows if o != "excused"]
        if not judged or any(o != "met" for o in judged):
            break
        streak += 1
    return streak


# ---- the reckoning ----

def reckon_period(db, charter: C.Charter, period: int, player_id: int) -> tuple[Snapshot, int, str | None]:
    _, end = _bounds(charter, period)
    status = db.execute("SELECT status FROM v_charter WHERE charter_id = ?", (charter.charter_id,)).fetchone()[0]
    outcomes = judge(db, charter, period)
    timeline = institutions.active_timeline(db)
    for term, required, delivered, outcome, note in outcomes:
        db.execute("INSERT INTO compliance (timeline_id, charter_id, period, term_key, required, delivered, outcome,"
                   " note, game_time) VALUES (?,?,?,?,?,?,?,?,?)",
                   (timeline, charter.charter_id, period, term, required, delivered, outcome, note, end))
    if not outcomes:   # keep the period counted even with no terms
        db.execute("INSERT INTO compliance (timeline_id, charter_id, period, term_key, outcome, note, game_time)"
                   " VALUES (?,?,?,'none','excused','no terms fell due',?)", (timeline, charter.charter_id, period, end))
    jarl_off = D.official("balgruuf")
    jarl_id = C.npc_for(db, jarl_off) if jarl_off else None
    eid = C.event(db, "lordship.reckoning", end, [player_id], {"charter_id": charter.charter_id, "period": period,
                                                               "outcomes": {o[0]: o[3] for o in outcomes}})
    paid, factor = settle(db, charter, period, outcomes, status, eid)
    snap = measure(db, charter, period, player_id, factor, outcomes)
    stances = [stance(db, o, snap, player_id) for o in D.officials()]
    for s in stances:
        write_stance(db, s, charter.holder, period, end)
    chosen = choose_move(charter, status, snap, stances, _clean_streak(db, charter, period))
    move_text = apply_move(db, charter, chosen[0], chosen[1], period, end, player_id) if chosen else None
    missed = len(snap.missed)
    if jarl_id and missed:
        _feel(db, jarl_id, player_id, {"trust": -0.04 * missed, "respect": -0.03 * missed},
              f"the Plains Wardens missed {', '.join(snap.missed)} under their charter", eid, end)
    elif jarl_id and any(o[3] == "met" for o in outcomes) and not snap.short:
        _feel(db, jarl_id, player_id, {"respect": 0.02}, "the Plains Wardens kept their charter this month", eid, end)
    db.execute("INSERT INTO influence_snapshot (timeline_id, charter_id, period, measures_json, power, legitimacy, gap,"
               " band, game_time) VALUES (?,?,?,?,?,?,?,?,?)",
               (timeline, charter.charter_id, period, json.dumps(snap.measures()), snap.power, snap.legitimacy,
                snap.gap, snap.band, end))
    return snap, paid, move_text


def catch_up(db, now: float, limit: int = MAX_CATCH_UP) -> Result:
    """Run every whole period of the live charter that has ended by ``now`` and not been reckoned on this save."""
    result = Result()
    charter = C.current(db)
    if charter is None:
        return result
    days = D.terms().period_days
    due = int((now - charter.signed_gt) // days)
    done = periods_done(db, charter)
    for period in range(done + 1, min(due, done + limit) + 1):
        charter = C.current(db) or charter
        if charter.status is None or not charter.live:
            break
        _, paid, move = reckon_period(db, charter, period, charter.holder_npc_id)
        result.periods += 1
        result.paid += paid
        if move:
            result.moves.append(move)
    latest = C.latest(db)
    result.status = latest.status if latest else None
    return result


def latest_snapshot(db, charter: C.Charter) -> tuple[int, dict, float, float, float, str] | None:
    r = db.execute("SELECT period, measures_json, power, legitimacy, gap, band FROM v_influence_snapshot"
                   " WHERE charter_id = ? ORDER BY period DESC, snap_id DESC LIMIT 1", (charter.charter_id,)).fetchone()
    return None if r is None else (r[0], json.loads(r[1]), r[2], r[3], r[4], r[5])
