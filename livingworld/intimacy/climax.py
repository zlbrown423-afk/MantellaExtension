"""Climaxes in an OStim scene, and the mess they leave (OCum draws it; we only read OStim's own event).

The game sends a ``climax`` record when someone in a followed scene climaxes (``ostim_actor_orgasm``), with
the OStim action types of the scene at that moment. For a man those tell where it most likely landed, the
way OCum Ascended places its overlays: a mouth act puts it on the partner's face, a breast act on the chest,
hands, feet or thighs on the body, sex inside. OCum's own choice is not read (its scripts send no event), so
this is a close guess, not a copy.

* Everyone in the scene remembers it; bystanders who could see remember seeing it.
* Afterwards, until the person it landed on bathes or swims (``hygiene``), or ``AFTERMATH_HOURS`` pass, the
  people around notice it where it shows: a face always, the body only when that person has nothing on the
  body, "inside" only as what runs down the thighs of someone with nothing on.

No relationship changes: the scene's own record (``scenes.py``) carries those. ``MANTELLA_LW_INTIMACY=0`` off.
"""
from __future__ import annotations

import json

from src import utils

from livingworld.intimacy import common
from livingworld.perception.formation import MemoryDraft
from livingworld.perception.records import PerceptionRecord

logger = utils.get_logger()

EVENT_CLIMAX = "intimacy.climax"
AFTERMATH_HOURS = 4.0
CLEANING = ("bath", "offscreen_bath", "swim")

# Where a man's climax lands, from the scene's OStim action types, most telling first.
_WHERE: list[tuple[str, tuple[str, ...]]] = [
    ("face", ("blowjob", "deepthroating", "penilelicking", "testicularlicking", "kissingpenis")),
    ("chest", ("boobjob", "suckingnipple", "lickingnipple", "gropingbreast")),
    ("inside", ("vaginalsex", "analsex")),
    ("body", ("handjob", "footjob", "thighjob", "buttjob", "grindingpenis", "malemasturbation", "grindingthigh")),
]
SHOWS_COVERED = {"face"}          # still seen with clothes on


def where(acts: list[str]) -> str:
    seen = set(acts)
    for spot, ids in _WHERE:
        if seen.intersection(ids):
            return spot
    return "body"


def _phrase(by: str, on: str, spot: str, *, mine: bool = False, on_me: bool = False) -> str:
    """'Torvar finished on Gerdur's face' / 'finished inside me'."""
    target = "me" if on_me else on
    possessive = "my" if on_me else f"{on}'s"
    if spot == "inside":
        return f"{by} finished inside {target}"
    if spot == "face":
        return f"{by} finished on {possessive} face"
    if spot == "chest":
        return f"{by} finished on {possessive} chest"
    return f"{by} finished on {target}"


def record_climax(pipeline, record: PerceptionRecord):
    from livingworld.perception.pipeline import Outcome
    existing = common.existing_event(pipeline.store, record.key)
    if existing is not None:
        return Outcome(existing, duplicate=True)
    gt = record.game_time
    by_id = pipeline.identities.ensure_npc(record.actor.identity(), gt)
    on_id = pipeline.identities.ensure_npc(record.target.identity(), gt)
    if by_id == on_id or pipeline._child(by_id) or pipeline._child(on_id):
        logger.log(23, f"Living World: climax record {record.key} skipped (one person, or a child)")
        return Outcome(None)
    by, on = record.actor.name, record.target.name
    male = record.sex == "male"
    spot = where(record.acts) if male else None
    seen, unseen = common.seers(pipeline, record, {by_id, on_id})
    payload = {"by": by_id, "on": on_id, "where": spot, "male": male, "acts": record.acts, "thread": record.thread,
               "place_name": record.location, "names": [by, on], "nearby_but_unseen": unseen}
    drafts: dict[int, tuple[MemoryDraft, list[int]]] = {}
    at = common.place(record)
    if male:
        drafts[by_id] = (MemoryDraft(f"{_phrase('I', on, spot)}{at}.", None, 0.55, 0.5, 0.6, 1.0, "experienced", {}), [])
        drafts[on_id] = (MemoryDraft(f"{_phrase(by, on, spot, on_me=True)}{at}.", None, 0.6, 0.3, 0.6, 1.0,
                                     "experienced", {}), [])
    else:
        drafts[by_id] = (MemoryDraft(f"I climaxed with {on}{at}.", None, 0.55, 0.6, 0.6, 1.0, "experienced", {}), [])
        drafts[on_id] = (MemoryDraft(f"{by} climaxed with me{at}.", None, 0.5, 0.5, 0.5, 1.0, "experienced", {}), [])
    with pipeline.store.transaction() as db:
        for npc_id, how in seen.items():
            what = _phrase(by, on, spot).replace(" finished", " finish", 1) if male else f"{by} climax with {on}"
            partner = common.partner_in(db, npc_id, [by_id, on_id])
            interpretation, valence = None, -0.05
            if partner is not None:
                interpretation = f"{by if partner == by_id else on} is mine, and that was with someone else."
                valence = -0.7
            drafts[npc_id] = (MemoryDraft(f"{common.hedge(how)} {what}{at}.", interpretation, 0.55, valence, 0.5,
                                          how.confidence, "witnessed", {}), [])
    outcome = common.store(pipeline, record, EVENT_CLIMAX, [by_id, on_id], payload,
                           {by_id: "participated", on_id: "participated"}, seen, drafts)
    logger.log(23, f"Living World: stored {EVENT_CLIMAX} event {outcome.event_id} ({record.key}): {by} with {on},"
                   f" where {spot or '-'}; memories for {len(outcome.memories)} NPC(s)")
    return outcome


# ---- the mess afterwards -------------------------------------------------------------------------

def marks(db, now_gt: float) -> list[dict]:
    """Recent climaxes that still show on someone: [{'on': id, 'on_name', 'by_name', 'where'}], newest first."""
    rows = db.execute(
        "SELECT payload_json, game_time FROM v_visible_event WHERE type = ? AND game_time >= ? AND game_time <= ?"
        " ORDER BY game_time DESC", (EVENT_CLIMAX, now_gt - AFTERMATH_HOURS / 24.0, now_gt + 1e-6)).fetchall()
    out, done = [], set()
    for payload_json, gt in rows:
        p = json.loads(payload_json)
        if not p.get("male") or not p.get("where"):
            continue
        on = p.get("on")
        if on is None or (on, p["where"]) in done:
            continue
        cleaned = db.execute(
            f"SELECT 1 FROM v_hygiene_change WHERE npc_id = ? AND game_time > ? AND game_time <= ?"
            f" AND cause IN ({','.join('?' * len(CLEANING))}) LIMIT 1", (on, gt, now_gt + 1e-6, *CLEANING)).fetchone()
        if cleaned:
            continue
        done.add((on, p["where"]))
        names = p.get("names") or ["someone", "someone"]
        out.append({"on": on, "on_name": names[1], "by_name": names[0], "where": p["where"]})
    return out


def _shows(spot: str, covered: bool) -> bool:
    return spot in SHOWS_COVERED or not covered


def _mess_words(spot: str, who: str) -> str:
    if spot == "face":
        return f"{who} has semen on their face"
    if spot == "chest":
        return f"{who} has semen on their chest"
    if spot == "inside":
        return f"semen is running down {who}'s thighs"
    return f"{who} has semen on their skin"


def lines_for(db, npc_id: int, name: str, now_gt: float, present: dict[int, str], attire: dict) -> list[str]:
    """What ``npc_id`` notices of the mess on the people around (and on themselves)."""
    lines = []
    for mark in marks(db, now_gt):
        on = mark["on"]
        state = attire.get(on)
        covered = state.covered if state is not None else True
        if on == npc_id:
            if _shows(mark["where"], covered):
                lines.append(f"{name} still has {mark['by_name']}'s semen on them from earlier"
                             f" ({'on their face' if mark['where'] == 'face' else 'where it shows'}) and knows it shows.")
            continue
        visible = on in present or (state is not None and npc_id in state.seen_by)
        if visible and _shows(mark["where"], covered):
            lines.append(f"{name} can see that {_mess_words(mark['where'], mark['on_name'])}.")
    return lines
