"""Change over time: small, capped, explained steps from what actually happened to her. No dice, no LLM.

Run lazily, like the rest of the simulation: whenever she is in a conversation (start and end), everything that
happened to her since her last checkpoint is read and turned into at most a few points of change per game day.
The inputs are the records other systems already keep, so nothing is counted twice:

* her own relationship changes toward anyone (``relationship_change``): trust and closeness gained, and trust lost;
  trust lost to being grabbed or handled (``physical_interaction`` events) counts as an unwanted experience;
* romance bond changes (``romance_bond_change``): becoming partners, betrothed or married, and breakups;
* intimate encounters she took part in (``encounter``), and how they left her feeling (satisfaction deltas).

Rules (points on the 0..100 scales; ``rate`` from the settings file scales them all):
* trust and closeness gained -> openness up, intimacy comfort up;
* satisfying intimacy -> sexual confidence up, a little initiation and (rarely) desire up;
* unwanted handling -> selectiveness up, intimacy comfort and openness down, and after enough of it the
  boundary "nobody touches her without asking first" is added (boundaries are only ever added, never removed);
* a bond becoming committed -> romance up; a breakup -> romance and openness down, selectiveness up.

Limits: at most ``MAX_PER_DAY`` points per attribute per game day elapsed (never less than one day's worth), and
never further from her start than the attribute's ``drift`` (``schema.py``). Values set by hand, MANUAL mode and
``evolution.enabled: false`` are never changed. Moods and arousal are not touched here: they are temporary
states kept by ``mood.py`` and ``intimacy/arousal.py``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from livingworld.personality import profile as P
from livingworld.personality.config import Settings

MAX_PER_DAY = 3.0
MIN_STEP = 0.2
UNWANTED_FOR_BOUNDARY = 0.35     # trust lost to unwanted handling since the last checkpoint
COMMITTED = ("partners", "betrothed", "married")


@dataclass
class Inputs:
    since: float
    now: float
    trust_gain: float = 0.0
    trust_loss: float = 0.0
    closeness_gain: float = 0.0     # intimacy + affinity gained
    satisfaction: float = 0.0
    unwanted: float = 0.0
    intimate_completed: int = 0
    committed: int = 0
    breakups: int = 0
    causes: list[str] = field(default_factory=list)


@dataclass
class Result:
    npc_id: int
    changes: list[tuple[str, str, float | str, str]] = field(default_factory=list)  # (attribute, kind, delta/item, reason)
    skipped: str = ""


def _checkpoint(db, npc_id: int) -> float | None:
    rows = P.changes_since_reset(db, npc_id)
    marks = [r["game_time"] for r in rows if r["kind"] in ("checkpoint", "reset")]
    if not marks:
        reset = db.execute("SELECT MAX(game_time) FROM v_personality_change WHERE npc_id = ? AND kind = 'reset'",
                           (npc_id,)).fetchone()[0]
        return reset
    return max(marks)


def gather(db, npc_id: int, since: float, now: float) -> Inputs:
    i = Inputs(since, now)
    for row in db.execute(
            "SELECT c.change_id, c.deltas_json, e.type FROM relationship_change c"
            " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
            " LEFT JOIN world_event e ON e.event_id = c.cause_event_id"
            " WHERE c.from_id = ? AND c.game_time > ? AND c.game_time <= ?", (npc_id, since, now + 1e-9)):
        try:
            d = json.loads(row[1] or "{}")
        except ValueError:
            continue
        trust = float(d.get("trust") or 0.0)
        i.trust_gain += max(0.0, trust)
        i.trust_loss += max(0.0, -trust)
        if row[2] == "physical_interaction" and trust < 0:
            i.unwanted += -trust
        i.closeness_gain += max(0.0, float(d.get("intimacy") or 0.0)) + 0.5 * max(0.0, float(d.get("affinity") or 0.0))
        i.satisfaction += float(d.get("satisfaction") or 0.0)
    for row in db.execute(
            "SELECT c.change_id, c.stage_to FROM romance_bond_change c"
            " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
            " WHERE (c.a_id = ? OR c.b_id = ?) AND c.game_time > ? AND c.game_time <= ?",
            (npc_id, npc_id, since, now + 1e-9)):
        if row[1] in COMMITTED:
            i.committed += 1
            i.causes.append(f"bond:{row[0]}")
        elif row[1] == "ended":
            i.breakups += 1
            i.causes.append(f"bond:{row[0]}")
    for row in db.execute(
            "SELECT encounter_id FROM v_encounter WHERE (a_id = ? OR b_id = ?) AND kind = 'intimate'"
            " AND outcome = 'completed' AND game_time > ? AND game_time <= ?", (npc_id, npc_id, since, now + 1e-9)):
        i.intimate_completed += 1
        i.causes.append(f"encounter:{row[0]}")
    return i


def proposals(i: Inputs) -> dict[str, tuple[float, str]]:
    """attribute -> (points, why) before limits."""
    out: dict[str, tuple[float, str]] = {}

    def add(key: str, pts: float, why: str) -> None:
        if abs(pts) < 1e-9:
            return
        old = out.get(key, (0.0, ""))
        out[key] = (old[0] + pts, (old[1] + "; " if old[1] else "") + why)

    if i.trust_gain > 0 or i.closeness_gain > 0:
        add("openness", 6 * i.trust_gain + 3 * i.closeness_gain, "trust and closeness grew")
        add("intimacy_comfort", 3 * i.trust_gain + 6 * i.closeness_gain, "trust and closeness grew")
    good = i.intimate_completed > 0 and i.satisfaction > 0
    if good:
        add("sexual_confidence", 2.0 * min(3, i.intimate_completed), "intimacy that left her satisfied")
        add("initiation", 0.8 * min(3, i.intimate_completed), "intimacy that left her satisfied")
        if i.intimate_completed >= 3:
            add("libido", 1.0, "a satisfying run of intimacy")
    elif i.intimate_completed > 0 and i.satisfaction < -0.1:
        add("sexual_confidence", -2.0, "intimacy that left her unhappy")
        add("intimacy_comfort", -2.0, "intimacy that left her unhappy")
    if i.unwanted > 0:
        add("selectiveness", 8 * i.unwanted, "unwanted handling")
        add("intimacy_comfort", -8 * i.unwanted, "unwanted handling")
        add("openness", -4 * i.unwanted, "unwanted handling")
    other_loss = max(0.0, i.trust_loss - i.unwanted)
    if other_loss > 0:
        add("openness", -3 * other_loss, "trust broken")
    if i.committed:
        add("romantic_inclination", 3.0 * i.committed, "a bond became committed")
        add("exclusivity", 1.5 * i.committed, "a bond became committed")
    if i.breakups:
        add("romantic_inclination", -3.0 * i.breakups, "a breakup")
        add("openness", -2.0 * i.breakups, "a breakup")
        add("selectiveness", 2.0 * i.breakups, "a breakup")
    return out


def evaluate(db, npc_id: int, settings: Settings, now: float, cause: str | None = None) -> Result:
    """Apply change over time for one woman up to ``now``. Call inside a transaction."""
    res = Result(npc_id)
    p = P.resolve(db, npc_id, settings, now)
    if not p.active:
        res.skipped = "no active profile"
        return res
    if p.mode == "manual" or not settings.evolution_enabled:
        res.skipped = "manual mode" if p.mode == "manual" else "evolution switched off"
        return res
    since = _checkpoint(db, npc_id)
    if since is None:      # first look: start counting from now; what happened before shaped the generated profile
        P.add_change(db, npc_id, "*", "checkpoint", "profile first seen", now, cause=cause)
        res.skipped = "first checkpoint"
        return res
    if now <= since + 1e-6:
        res.skipped = "nothing new"
        return res
    inputs = gather(db, npc_id, since, now)
    cap = MAX_PER_DAY * max(1.0, now - since) * settings.evolution_rate
    where = ", ".join(([cause] if cause else []) + inputs.causes[:4]) or None
    from livingworld.personality import schema
    for key, (pts, why) in proposals(inputs).items():
        if p.locked(key):
            continue
        pts = max(-cap, min(cap, pts * settings.evolution_rate))
        limit = schema.BY_KEY[key].drift
        room_up = limit - p.drift.get(key, 0.0)
        room_down = -limit - p.drift.get(key, 0.0)
        pts = max(room_down, min(room_up, pts))
        value = p.values.get(key, 50.0)
        pts = max(-value, min(100.0 - value, pts))
        if abs(pts) < MIN_STEP:
            continue
        pts = round(pts, 2)
        P.add_change(db, npc_id, key, "drift", why, now, delta=pts, cause=where)
        res.changes.append((key, "drift", pts, why))
    if inputs.unwanted >= UNWANTED_FOR_BOUNDARY and not p.locked("boundaries") \
            and "no_unasked_touch" not in p.values.get("boundaries", []):
        why = "after being handled without wanting it, she wants to be asked first"
        P.add_change(db, npc_id, "boundaries", "list_add", why, now, value="no_unasked_touch", cause=where)
        res.changes.append(("boundaries", "list_add", "no_unasked_touch", why))
    P.add_change(db, npc_id, "*", "checkpoint", "looked at what happened", now, cause=cause)
    return res
