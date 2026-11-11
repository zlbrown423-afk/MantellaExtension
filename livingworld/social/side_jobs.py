"""Side jobs: the player sends a companion out in Whiterun to steal valuables or to flirt men out of their coin.

Built for Salonia Carvain once she has surrendered and come along (``surrender.py``): anyone who surrendered to
the player can be asked. The player asks in words ("go and lift some jewellery", "flirt the guards for coin"),
or with the dialogue choice the game plugin adds (a ``job`` record, below). Nothing happens at once:

1. **Scouting.** She agrees and spends a few hours making connections and watching who has what
   (``SCOUT_DAYS``). When the conversation ends she wanders off to the market (``TravelTo``).
2. **Proposal.** The next time the player talks to her alone (or brings it up), she puts up to three marks to
   them, each with what there is to take and how risky it is, and the game shows them as numbered choices.
   Marks come from ``data/side_job_marks.json`` (Whiterun shops, rich men, the guards); a mark who caught
   her before is left out.
3. **Approval.** The player picks one ("the first one", "Belethor"), or calls it off ("no", "forget it").
   Nothing is ever started without that pick. On the pick, her next line carries ``TravelTo`` to the mark's
   place (it starts after the conversation), so the player can follow and watch.
4. **The job.** A theft is carried out either
   * with cover: the player starts a conversation with the shop owner while the job is on (within
     ``COVER_DAYS``) and she is not in it; she lifts the goods then, with better odds; or
   * on her own: once ``JOB_DAYS`` have passed, the next time the player talks to her.
   A flirting job always happens on her own. The outcome is rolled here (seeded by the job, so it is the same
   on every retry) and sent to the game in ``mantella_lw_side_job_do``; the game moves the actual items
   (worth ``MIN_VALUE`` gold or more) or the coin into her pack. Being caught never adds a bounty: the guards
   only get a BOLO (below). It answers with a ``job`` record ``via=loot`` naming what was really taken.
5. **Report.** She tells the player how it went the next time they are alone (or the player asks), and
   offers to hand the loot over.

She never raises any of it in front of other people; when the player brings it up there, she lowers her
voice and keeps it short. Outcomes become memories (hers, and the mark's when she was caught or flirted with
them) and relationship changes (a mark who caught her trusts her, and a player who covered for her, less).

Everything is stored as ``side_job`` world events (one per stage), so loading an older save rolls the job
back with it. ``MANTELLA_LW_SALONIA_JOBS=0`` switches it off (the start reply's ``mantella_lw_side_jobs_on``
tells the game).

Game records (Mantella-Spell queues them like every perception record)::

    v=1|key=..|type=job|gt=4.2|loc=Whiterun|actor=<her>|target=<player>|via=ask|task=theft    (or flirt)
    v=1|key=..|type=job|gt=4.6|loc=Whiterun|actor=<her>|target=<player>|via=approve|task=2   (pick mark 2)
    v=1|key=..|type=job|gt=4.6|loc=Whiterun|actor=<her>|target=<player>|via=drop
    v=1|key=..|type=job|gt=4.9|loc=Whiterun|actor=<her>|via=loot|task=<job id>|loot=Gold Ring:75;Silver Necklace:60|gold=0

Order to the game (a list, one string per job)::

    job id|theft or flirt|ok, caught or nothing|her name|her ref (plugin:0xID or empty)|owner name|owner ref|
    min value|max items|gold|bounty (always 0)|bolo (1 = guards on the lookout)

Nobody is ever wanted for a side job. A job nobody saw leaves no trace with the guards at all. A theft she is
caught at only puts out a BOLO: for ``BOLO_DAYS`` the Whiterun guards know to look out for her (and that the
player was keeping the owner talking, if so); a guard who meets her eyes her and may question her, but does not
arrest, fine or attack anyone over it.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from livingworld import mood as moods

ENV = "MANTELLA_LW_SALONIA_JOBS"
KEY_JOBS_ON = "mantella_lw_side_jobs_on"
KEY_JOB_DO = "mantella_lw_side_job_do"
EVENT = "side_job"
THEFT, FLIRT = "theft", "flirt"
MIN_VALUE = 30
MAX_ITEMS = 2
MAX_MARKS = 3
SCOUT_DAYS = 0.25        # six hours of game time to make connections and watch
JOB_DAYS = 0.25          # an approved job done on her own is over this long afterwards
BOLO_DAYS = 3.0          # guards look out for her this long after she was caught
COVER_DAYS = 1.0         # the player can cover for her by talking to the owner within this long
TRAVEL_TO = "mantella_npc_travelto"
_OFF = {"0", "false", "no", "off"}
_RISK = {"low": 0.0, "medium": 0.15, "high": 0.3}

logger = logging.getLogger("livingworld.side_jobs")
_MARKS_FILE = Path(__file__).resolve().parent / "data" / "side_job_marks.json"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


@lru_cache(maxsize=1)
def marks_catalogue() -> dict[str, list[dict[str, Any]]]:
    with open(_MARKS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {THEFT: data[THEFT], FLIRT: data[FLIRT]}


def _mark(kind: str, key: str) -> dict[str, Any] | None:
    return next((m for m in marks_catalogue().get(kind, []) if m["key"] == key), None)


# ---------------------------------------------------------------- where

_WHITERUN_PLACES = {"whiterun", "dragonsreach", "breezehome", "jorrvaskr", "the bannered mare", "bannered mare",
                    "belethor's general goods", "arcadia's cauldron", "warmaiden's", "the drunken huntsman",
                    "house gray-mane", "house of clan battle-born", "the skyforge", "whiterun marketplace",
                    "whiterun stables", "carlotta valentia's house", "uthgerd's house", "ysolda's house",
                    "heimskr's house", "amren's house", "severio pelagia's house", "olava the feeble's house",
                    "hall of the dead", "temple of kynareth", "the gildergreen", "chillfurrow farm",
                    "battle-born farm", "honningbrew meadery", "pelagia farm"}


def in_whiterun(location: str | None) -> bool:
    loc = (location or "").strip().lower().replace("’", "'")
    return "whiterun" in loc or loc in _WHITERUN_PLACES or any(p in loc for p in ("bannered mare", "dragonsreach",
                                                                                    "breezehome", "jorrvaskr"))


# ---------------------------------------------------------------- words

_NEG = r"(?:don'?t|do\s+not|never|stop|no\s+more|quit)\b[^.!?]{0,20}"
_THEFT = re.compile(r"\b(?:steal\w*|stole|lift(?:ing)?|pinch(?:ing)?|nick(?:ing)?|swip(?:e|ing)|pilfer\w*|filch\w*"
                    r"|pickpocket\w*|pick\s+(?:some\s+)?pockets|rob(?:bing)?|loot(?:ing)?|pillag\w*|burgl\w*|thiev\w*"
                    r"|five[\s-]finger\w*|heist)\b", re.IGNORECASE)
_FLIRT = re.compile(r"\b(?:flirt\w*|sweet[\s-]?talk\w*|seduc\w*|charm(?:ing)?|tease|work\s+the\s+(?:men|guards|room))\b",
                    re.IGNORECASE)
_FOR_COIN = re.compile(r"\b(?:coin|coins|gold|money|septims?|men|man|guards?|tips?|purse)\b", re.IGNORECASE)
_SCOUT = re.compile(r"\b(?:scope|scout\w*|case|recon\w*|connections|marks?|targets?|plan|job)\b", re.IGNORECASE)
_DROP = re.compile(r"^\W*(?:no|nope|nah)\b|\b(?:call\s+it\s+off|forget\s+(?:it|about\s+it)|never\s*mind|cancel"
                   r"|scrap\s+it|drop\s+it|don'?t\s+do\s+it|not\s+doing\s+it|none\s+of\s+them)\b", re.IGNORECASE)
_YES = re.compile(r"\b(?:yes|yeah|aye|do\s+it|go\s+ahead|go\s+for\s+it|sounds\s+good|that\s+one|approved?)\b",
                  re.IGNORECASE)
_ORDINALS = [r"\b(?:one|first|1|number\s+one)\b", r"\b(?:two|second|2|number\s+two)\b",
             r"\b(?:three|third|3|number\s+three)\b"]
_COVER = re.compile(r"\b(?:distract\w*|keep\s+(?:him|her|them)\s+(?:talking|busy)|cover\s+(?:for\s+)?you"
                    r"|i'?ll\s+(?:talk|chat)\s+to|i'?ll\s+keep\s+watch|watch\s+your\s+back|look\s*out|lookout)\b",
                    re.IGNORECASE)


_SEND = re.compile(r"\b(?:go(?:\s+and)?|want\s+you\s+to|need\s+you\s+to|could\s+you|can\s+you|would\s+you|will\s+you"
                   r"|try\s+(?:to|and)|i'?d\s+like\s+you\s+to|how\s+about\s+you|time\s+to|let'?s|head\s+out\s+and"
                   r"|scope\s+out|scout)\b", re.IGNORECASE)
_PAST = re.compile(r"^\W*(?:did|have|has|were|was|why\s+did|who)\b", re.IGNORECASE)


def player_asks(text: str) -> str | None:
    """THEFT or FLIRT when the player's line sends her out on a job, else None."""
    t = text or ""
    if re.search(_NEG + r"(?:steal|lift|rob|flirt|loot|pickpocket)", t, re.IGNORECASE) or _PAST.search(t):
        return None
    if not _SEND.search(t):
        return None
    if _FLIRT.search(t) and _FOR_COIN.search(t):
        return FLIRT
    if _THEFT.search(t):
        return THEFT
    return None


def brings_up(text: str) -> bool:
    t = text or ""
    return bool(_THEFT.search(t) or _FLIRT.search(t) or re.search(r"\b(?:the\s+(?:job|plan|marks?|targets?)|your\s+plan"
                                                                     r"|how\s+did\s+it\s+go|scouting)\b", t, re.IGNORECASE))


def player_picks(text: str, marks: list[dict[str, Any]]) -> int | None | str:
    """The mark index (0-based) the player picked, 'drop' when they call it off, None when it is not an answer."""
    t = (text or "").lower().replace("’", "'").strip()
    if _DROP.search(t) and not _YES.search(t):
        return "drop"
    if t.endswith("?"):
        return None      # "which one is safest?", "what about Belethor?": still talking it over
    for i, m in enumerate(marks):
        names = {m["owner"].lower(), m["owner"].split(" ")[0].lower(), m["place"].lower()}
        if any(n and re.search(r"\b" + re.escape(n) + r"\b", t) for n in names):
            return i
    for i, pattern in enumerate(_ORDINALS[:len(marks)]):
        if re.search(pattern, t):
            return i
    if len(marks) == 1 and _YES.search(t):
        return 0
    return None


def offers_cover(text: str) -> bool:
    return bool(_COVER.search(text or ""))


# ---------------------------------------------------------------- state

@dataclass
class Job:
    job_id: int
    npc_id: int
    player_id: int
    kind: str
    asked_gt: float
    ready_gt: float
    marks: list[dict[str, Any]]
    pick: int | None = None
    approved_gt: float | None = None
    cover: bool = False
    outcome: dict[str, Any] | None = None
    loot: dict[str, Any] | None = None
    reported: bool = False
    dropped: bool = False

    @property
    def mark(self) -> dict[str, Any] | None:
        return self.marks[self.pick] if self.pick is not None and self.pick < len(self.marks) else None

    @property
    def stage(self) -> str:
        if self.dropped:
            return "dropped"
        if self.reported:
            return "reported"
        if self.outcome is not None:
            return "done"
        if self.pick is not None:
            return "approved"
        return "scouting"

    @property
    def open(self) -> bool:
        return self.stage in ("scouting", "approved", "done")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write(db, timeline: int, npc: int, player: int, payload: dict[str, Any], gt: float,
           location: str | None = None, key: str | None = None) -> int:
    return db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time, game_key,"
        " real_time, confidence) VALUES (?,?,?,?,?,?,?,?,?,1.0)",
        (timeline, EVENT, location, json.dumps([npc, player]), json.dumps(payload), "sim", gt, key,
         _now_iso())).lastrowid


def _timeline(db) -> int:
    row = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()
    return row[0] if row else 1


def jobs(db, now: float, npc: int | None = None) -> list[Job]:
    """Every side job visible on the active timeline at ``now`` (oldest first), built from its stage events."""
    found: dict[int, Job] = {}
    for event_id, actors_json, payload_json, gt in db.execute(
            "SELECT event_id, actors_json, payload_json, game_time FROM v_visible_event WHERE type = ?"
            " AND game_time <= ? ORDER BY game_time, event_id", (EVENT, now + 1e-9)).fetchall():
        p = json.loads(payload_json or "{}")
        actors = json.loads(actors_json)
        stage = p.get("stage")
        if stage == "asked":
            found[event_id] = Job(event_id, actors[0], actors[1], p["kind"], gt, p["ready_gt"], p.get("marks") or [])
            continue
        job = found.get(p.get("job"))
        if job is None:
            continue
        if stage == "approved":
            job.pick, job.approved_gt, job.cover = p["pick"], gt, bool(p.get("cover"))
        elif stage == "cover":
            job.cover = True
        elif stage == "done":
            job.outcome = p.get("outcome") or {}
        elif stage == "loot":
            job.loot = {"items": p.get("items") or [], "gold": p.get("gold") or 0}
        elif stage == "reported":
            job.reported = True
        elif stage == "dropped":
            job.dropped = True
    out = list(found.values())
    return [j for j in out if npc is None or j.npc_id == npc]


def current(db, npc: int, now: float) -> Job | None:
    """Her open job (scouting, approved, or done and not yet reported), if any."""
    open_jobs = [j for j in jobs(db, now, npc) if j.open]
    return open_jobs[-1] if open_jobs else None


def _caught_by(db, npc: int, now: float) -> set[str]:
    return {j.mark["key"] for j in jobs(db, now, npc) if j.outcome and j.outcome.get("result") == "caught" and j.mark}


def ask(db, npc: int, player: int, kind: str, gt: float, location: str | None = None,
        key: str | None = None) -> tuple[Job, bool]:
    """She takes the job on: (job, True), or (her job already open, False)."""
    existing = current(db, npc, gt)
    if existing is not None:
        return existing, False
    caught = _caught_by(db, npc, gt)
    pool = [m for m in marks_catalogue()[kind] if m["key"] not in caught]
    rnd = random.Random(f"{npc}:{kind}:{round(gt, 3)}")
    marks = sorted(rnd.sample(pool, min(MAX_MARKS, len(pool))), key=lambda m: _RISK[m["risk"]])
    payload = {"stage": "asked", "kind": kind, "ready_gt": gt + SCOUT_DAYS, "marks": marks}
    job_id = _write(db, _timeline(db), npc, player, payload, gt, location, key)
    logger.log(23, f"Living World side jobs: job {job_id} ({kind}) taken on, marks "
                   f"{', '.join(m['owner'] for m in marks)}; ready at day {gt + SCOUT_DAYS:.2f}")
    return Job(job_id, npc, player, kind, gt, gt + SCOUT_DAYS, marks), True


def approve(db, job: Job, pick: int, gt: float, cover: bool = False) -> None:
    _write(db, _timeline(db), job.npc_id, job.player_id, {"stage": "approved", "job": job.job_id, "pick": pick,
                                                            "cover": cover}, gt)
    job.pick, job.approved_gt, job.cover = pick, gt, cover
    logger.log(23, f"Living World side jobs: job {job.job_id} approved: {job.marks[pick]['owner']}"
                   f"{' (the player covers)' if cover else ''}")


def note_cover(db, job: Job, gt: float) -> None:
    if not job.cover:
        _write(db, _timeline(db), job.npc_id, job.player_id, {"stage": "cover", "job": job.job_id}, gt)
        job.cover = True


def drop(db, job: Job, gt: float) -> None:
    _write(db, _timeline(db), job.npc_id, job.player_id, {"stage": "dropped", "job": job.job_id}, gt)
    job.dropped = True
    logger.log(23, f"Living World side jobs: job {job.job_id} called off")


def mark_reported(db, job: Job, gt: float) -> None:
    _write(db, _timeline(db), job.npc_id, job.player_id, {"stage": "reported", "job": job.job_id}, gt)
    job.reported = True


def record_loot(db, job: Job, items: list[dict[str, Any]], gold: int, gt: float, key: str | None = None) -> None:
    _write(db, _timeline(db), job.npc_id, job.player_id, {"stage": "loot", "job": job.job_id, "items": items,
                                                            "gold": gold}, gt, key=key)
    job.loot = {"items": items, "gold": gold}
    logger.log(23, f"Living World side jobs: job {job.job_id} loot from the game: "
                   f"{', '.join(i['name'] for i in items) or 'nothing'}{f', {gold} gold' if gold else ''}")


# ---------------------------------------------------------------- the job itself

@dataclass
class Order:
    """What the game carries out for a finished job."""
    job_id: int
    kind: str
    result: str          # ok | caught | nothing
    npc_name: str
    npc_ref: str
    owner: str
    owner_ref: str
    max_items: int
    gold: int
    bounty: int          # always 0: kept so the game's field order stays fixed
    bolo: int = 0        # 1: the Whiterun guards are on the lookout for her (nobody is wanted)

    def encode(self) -> str:
        return "|".join(str(x) for x in (self.job_id, self.kind, self.result, self.npc_name, self.npc_ref, self.owner,
                                          self.owner_ref, MIN_VALUE, self.max_items, self.gold, self.bounty, self.bolo))


def _ref_of(db, npc_id: int | None) -> str:
    if npc_id is None:
        return ""
    row = db.execute("SELECT plugin, ref_form_id, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row:
        return ""
    ref = row[1] if row[1] and "|" in row[1] else f"{row[0]}|{row[1] or row[2]}"   # 'Skyrim.esm|0x00013BA0'
    return ref.replace("|", ":")


def _npc_named(db, name: str) -> int | None:
    row = db.execute("SELECT npc_id FROM npc WHERE display_name = ? AND is_player = 0 ORDER BY npc_id LIMIT 1",
                     (name,)).fetchone()
    return row[0] if row else None


def _name(db, npc_id: int) -> str:
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else "someone"


def _memory(db, timeline: int, owner: int, content: str, about: list[int], location: str | None, gt: float,
            valence: float, importance: float, interpretation: str, event_id: int) -> None:
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
        " VALUES (?, ?, 'episodic', ?, ?, ?, ?, ?, ?, 0.7, 'perception', 'experienced', ?, 1.0, ?)",
        (timeline, owner, content, json.dumps(about), location, gt, importance, valence, interpretation,
         event_id)).lastrowid
    db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))


def _relate(db, timeline: int, from_id: int, to_id: int, deltas: dict[str, float], event_id: int, reason: str,
            gt: float) -> None:
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
               (from_id, to_id, "acquaintance", gt))
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)", (timeline, from_id, to_id, json.dumps(deltas), event_id, reason, gt))


def resolve(db, job: Job, gt: float, covered: bool) -> Order:
    """Roll how the job went (the same roll every time for this job), store it and its memories, and return
    the order for the game. ``covered``: the player was talking to the owner while she worked."""
    mark = job.mark or {}
    rnd = random.Random(f"side-job:{job.job_id}")
    risk = _RISK.get(mark.get("risk", "medium"), 0.15)
    wealth = int(mark.get("wealth", 1))
    owner_name = mark.get("owner", "someone")
    place = mark.get("place")
    owner_id = _npc_named(db, owner_name) if owner_name != "Whiterun Guard" else None
    if job.kind == THEFT:
        chance = 0.6 + (0.25 if covered else 0.0) - risk
        ok = rnd.random() < chance
        result = "ok" if ok else "caught"
        gold = rnd.randint(20, 40) * wealth if ok and job.marks[job.pick]["key"] in ("hulda", "carlotta", "nazeem") else 0
        items = (2 if wealth >= 2 and rnd.random() < 0.5 else 1) if ok else 0
        bounty = 0       # never a real bounty: a theft she is caught at only puts the guards on the lookout (BOLO)
    else:
        chance = 0.85 - risk
        ok = rnd.random() < chance
        result = "ok" if ok else "caught"
        gold = rnd.randint(8, 25) * wealth + (rnd.randint(5, 15) if ok and mark.get("key") == "guards" else 0) if ok else 0
        items, bounty = 0, 0
    bolo = job.kind == THEFT and not ok
    outcome = {"result": result, "covered": covered, "gold": gold, "items": items, "bounty": bounty,
               "bolo": bolo, "owner": owner_name, "place": place}
    timeline = _timeline(db)
    event_id = _write(db, timeline, job.npc_id, job.player_id, {"stage": "done", "job": job.job_id, "outcome": outcome},
                      gt, place)
    job.outcome = outcome
    me, player = job.npc_id, job.player_id
    player_name = _name(db, player)
    if job.kind == THEFT:
        if ok:
            how = f" while {player_name} kept {owner_name} talking" if covered else ""
            _memory(db, timeline, me, f"I stole from {owner_name} at {place}{how}, and nobody saw.", [player],
                    place, gt, 0.4, 0.6, "a good haul, and my nerve held", event_id)
            moods.save(db, me, moods.Mood("pleased", 7), gt)
        else:
            _memory(db, timeline, me, f"{owner_name} caught me trying to steal at {place}. I got away, but"
                    f" {owner_name} knows my face now.", [player] + ([owner_id] if owner_id else []), place, gt,
                    -0.6, 0.8, "I was careless and it nearly cost me", event_id)
            moods.save(db, me, moods.Mood("shaken", 3), gt)
            if owner_id is not None:
                _memory(db, timeline, owner_id, f"I caught {_name(db, me)} with her hand on my goods at {place}.",
                        [me], place, gt, -0.7, 0.8, "a thief in my own shop", event_id)
                _relate(db, timeline, owner_id, me, {"trust": -0.4, "affinity": -0.2}, event_id,
                        "caught her stealing", gt)
                if covered:
                    _memory(db, timeline, owner_id, f"{player_name} kept me talking while {_name(db, me)} tried to"
                            f" rob me. I don't think that was chance.", [player, me], place, gt, -0.6, 0.7,
                            "they were working together", event_id)
                    _relate(db, timeline, owner_id, player, {"trust": -0.3}, event_id, "covered for a thief", gt)
    else:
        if ok:
            _memory(db, timeline, me, f"I flirted with {owner_name} at {place} for coin and came away with"
                    f" {gold} septims.", [player], place, gt, 0.3, 0.5, "easy money, if a little tiring", event_id)
            moods.save(db, me, moods.Mood("pleased", 7), gt)
            if owner_id is not None:
                _memory(db, timeline, owner_id, f"{_name(db, me)} flirted with me at {place}; I gave her some coin.",
                        [me], place, gt, 0.4, 0.5, "she was good company", event_id)
                _relate(db, timeline, owner_id, me, {"affinity": 0.1}, event_id, "flirted with him", gt)
        else:
            _memory(db, timeline, me, f"Flirting with {owner_name} at {place} went badly: I was seen through and"
                    f" sent off with nothing.", [player], place, gt, -0.4, 0.5, "humiliating", event_id)
            moods.save(db, me, moods.Mood("annoyed", 4), gt)
            if owner_id is not None:
                _memory(db, timeline, owner_id, f"{_name(db, me)} tried to sweet-talk coin out of me at {place}.",
                        [me], place, gt, -0.4, 0.5, "a schemer", event_id)
                _relate(db, timeline, owner_id, me, {"trust": -0.2}, event_id, "tried to flirt coin out of him", gt)
    logger.log(23, f"Living World side jobs: job {job.job_id} ({job.kind}) at {place}: {result}"
                   f"{' with the player covering' if covered else ''}, gold {gold}, items {items}, bounty {bounty}")
    return Order(job.job_id, job.kind, result, _name(db, me), _ref_of(db, me), owner_name, _ref_of(db, owner_id),
                 items, gold, bounty, int(bolo))


# ---------------------------------------------------------------- what she says

def why(db, me: int, name: str, player_id: int, player_name: str, now: float) -> str:
    """Why she does it at all: the player makes her. When the player killed her party, helping makes her their
    accomplice, and that is what keeps her quiet about the killings. Compelled, never eager."""
    from livingworld.social.surrender import _join, _latest, _names
    found = _latest(db, me, player_id, now)
    if found is not None and found[0].get("killer") == "player":
        lost = _join(_names(db, found[0].get("dead") or [])) or "her husband and bodyguard"
        return (f" {name} does this because {player_name} makes {name} do it, not because {name} wants to:"
                f" {player_name} killed {lost}, and every job {name} does for {player_name} makes {name} an accomplice,"
                f" which keeps {name} quiet about the killings. {name} knows it and so does {player_name}. {name} is"
                f" compelled, not eager: the fear and resentment stay under the surface, but {name} does the work"
                f" well, because {name} has no choice.")
    return (f" {name} does this because {player_name} makes {name} do it, not because {name} wants to; {name}"
            f" depends on {player_name} and has nowhere else to go.")

_KIND = {THEFT: "things worth stealing (worth 30 gold or more: jewellery first)",
         FLIRT: "men and guards she can flirt out of their coin"}


def _mark_text(i: int, m: dict[str, Any], kind: str) -> str:
    what = "take" if kind == THEFT else "get"
    return f"{i + 1}) {m['owner']} at {m['place']}: {what} {m['loot']}; {m['note']}; risk {m['risk']}"


def _report(job: Job, name: str, player_name: str) -> str:
    o = job.outcome or {}
    m = job.mark or {}
    owner, place = m.get("owner", "the mark"), m.get("place", "Whiterun")
    loot = job.loot or {}
    items = loot.get("items") or []
    gold = loot.get("gold", o.get("gold", 0)) if job.loot is not None else o.get("gold", 0)
    hand = (f" The takings are in {name}'s pack; {name} offers to hand them to {player_name} (use Inventory if"
            f" {player_name} wants them).")
    if o.get("result") == "caught":
        if job.kind == THEFT:
            extra = (f" {owner} saw {player_name} talking to them at the same time and may suspect both of them."
                     if o.get("covered") else "")
            extra += (" The guards have been told to keep an eye out for her for a few days, but nobody is wanted"
                      " and there is no bounty.")
            return (f"{name} has news for {player_name} about {owner} at {place}: it went wrong. {owner} caught"
                    f" {name} in the act; {name} got away empty-handed but shaken, and {owner} knows {name}'s face"
                    f" now, so {name} will not go back there.{extra} {name} tells it in {name}'s own words.")
        return (f"{name} has news for {player_name}: flirting with {owner} at {place} went badly and {name} came"
                f" away with nothing but a bruised pride. {name} tells it in {name}'s own words.")
    if job.kind == THEFT:
        if job.loot is not None and not items and not gold:
            return (f"{name} has news for {player_name}: {name} got into {place} unseen but found nothing worth"
                    f" taking within reach, and came away empty-handed. {name} tells it in {name}'s own words.")
        got = ", ".join(f"{i['name']} ({i.get('value', '?')} gold)" for i in items) if items else (
            "a piece or two of jewellery" if o.get("items") else "")
        if gold:
            got = (got + " and " if got else "") + f"{gold} septims"
        cover = f" with {player_name} keeping {owner} busy" if o.get("covered") else ""
        return (f"{name} has news for {player_name}: the job at {place} went well{cover}. {name} took {got or 'something'}"
                f" from {owner} and nobody saw. {name} is pleased with {name}'s self and tells it in {name}'s own"
                f" words." + hand)
    return (f"{name} has news for {player_name}: {name} spent time flirting with {owner} at {place} and came away"
            f" with {gold} septims. {name} tells it in {name}'s own words, a little smug about it." + hand)


def line(db, me: int, name: str, player_id: int, player_name: str, private: bool, brought_up: bool,
         now: float) -> tuple[str | None, Job | None]:
    """What ``name`` knows about her side job right now, and the job when this line reports its outcome."""
    text, job = _line(db, me, name, player_id, player_name, private, brought_up, now)
    if text and (private or brought_up):
        text += why(db, me, name, player_id, player_name, now)
    return text, job


def _line(db, me: int, name: str, player_id: int, player_name: str, private: bool, brought_up: bool,
          now: float) -> tuple[str | None, Job | None]:
    job = current(db, me, now)
    if job is None:
        return None, None
    quiet = (f"Others are listening: {name} never mentions the job in front of them; if {player_name} raises it,"
             f" {name} lowers {name}'s voice and keeps it to a few vague words, or suggests talking later alone.")
    if not private and not brought_up:
        return (f"{name} has a secret side job going with {player_name} in Whiterun. {quiet}"), None
    hush = "" if private else f" {quiet}"
    if job.stage == "scouting":
        if now < job.ready_gt:
            return (f"{name} agreed to look for {_KIND[job.kind]} in Whiterun for {player_name}. {name} is still"
                    f" making connections and watching who has what, and will put a plan to {player_name} once"
                    f" {name} has one, in a few hours. {name} does nothing yet.{hush}"), None
        marks = "; ".join(_mark_text(i, m, job.kind) for i, m in enumerate(job.marks))
        return (f"{name} has finished scouting Whiterun for {_KIND[job.kind]} and has a plan for {player_name}."
                f" The marks: {marks}. {name} brings it up quietly, describes each in {name}'s own words, says which"
                f" {name} favours and why, and asks {player_name} to pick one or call it off. {name} does nothing"
                f" until {player_name} picks."
                + (f" {name} would welcome {player_name} keeping the owner talking while {name} works." if
                   job.kind == THEFT else "") + hush), None
    if job.stage == "approved":
        m = job.mark
        cover = (f" {player_name} has offered to keep {m['owner']} talking as cover." if job.cover else
                 (f" {player_name} could cover for {name} by talking to {m['owner']} while {name} works." if
                  job.kind == THEFT else ""))
        return (f"{player_name} approved {name}'s plan: {m['owner']} at {m['place']}. {name} will go and do it as"
                f" soon as they part, alone.{cover}{hush}"), None
    return _report(job, name, player_name) + hush, job


def bolo_line(db, guard_name: str, present: dict[int, str], player_id: int, now: float) -> str | None:
    """What a Whiterun guard knows about a recent BOLO, or None. Never grounds for an arrest."""
    found = [j for j in jobs(db, now) if j.outcome and j.outcome.get("bolo") and j.player_id == player_id]
    recent = [j for j in found if now - _done_gt(db, j) <= BOLO_DAYS]
    if not recent:
        return None
    j = recent[-1]
    her = _name(db, j.npc_id)
    place = j.outcome.get("place") or "town"
    text = (f"{guard_name} has been told to keep an eye out for {her}, a woman seen trying to steal at {place}."
            f" It is only a lookout: nobody saw her take anything, she is not wanted and there is no bounty.")
    if j.npc_id in present:
        text += (f" {her} is right here: {guard_name} recognises her, eyes her with suspicion and may ask a pointed"
                 f" question or tell her to keep her hands to herself, but does not arrest, fine or attack her.")
    if j.outcome.get("covered") and player_id in present:
        text += (f" Word is that {_name(db, player_id)} was keeping the owner talking at the time; {guard_name} may"
                 f" let on that they have heard, but accuses nobody of anything.")
    return text


def _done_gt(db, job: Job) -> float:
    for payload_json, gt in db.execute("SELECT payload_json, game_time FROM v_visible_event WHERE type = ?"
                                       " ORDER BY game_time DESC", (EVENT,)).fetchall():
        p = json.loads(payload_json or "{}")
        if p.get("job") == job.job_id and p.get("stage") == "done":
            return gt
    return job.asked_gt


# ---------------------------------------------------------------- one conversation

@dataclass
class Desk:
    """Side jobs in one conversation: the player's words, the game's notices, orders and the trip to the mark."""
    notices: list[str] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    travel: dict[str, str] = field(default_factory=dict)     # her name -> TravelTo place for her next line
    proposed: set[int] = field(default_factory=set)          # jobs whose marks were shown this conversation
    told: dict[int, Job] = field(default_factory=dict)       # jobs whose outcome she was given to report
    brought_up: bool = False

    # ---- conversation start: jobs that happen now
    def started(self, db, player: int, present: dict[int, str], now: float) -> None:
        """A theft whose owner the player is talking to (without her) happens now, with cover; any job of
        someone present that has run its course happens now on her own."""
        for job in jobs(db, now):
            if job.player_id != player or job.stage != "approved" or job.approved_gt is None:
                continue
            here = job.npc_id in present
            owner = (job.mark or {}).get("owner", "")
            owner_here = any(n == owner for i, n in present.items() if i != job.npc_id)
            if job.kind == THEFT and not here and owner_here and now - job.approved_gt <= COVER_DAYS:
                self.orders.append(resolve(db, job, now, covered=True))
            elif here and now - job.approved_gt >= JOB_DAYS:
                self.orders.append(resolve(db, job, now, covered=False))

    # ---- the player's words
    def player_said(self, db, text: str, me: int, name: str, player: int, private: bool, location: str | None,
                    now: float) -> str | None:
        """A note for the dialogue model about what the player just asked or decided, or None."""
        first = name.split(" ")[0]
        job = current(db, me, now)
        if brings_up(text):
            self.brought_up = True
        if job is not None and job.stage == "scouting" and job.job_id in self.proposed:
            pick = player_picks(text, job.marks)
            if pick == "drop":
                drop(db, job, now)
                self.notices.append(f"{first}'s plan is called off.")
                return f"The player called off {name}'s plan. {name} lets it go."
            if isinstance(pick, int):
                cover = offers_cover(text)
                approve(db, job, pick, now, cover)
                m = job.marks[pick]
                self.travel[name] = m["place"]
                if job.kind == THEFT:
                    self.notices.append(f"{first} goes after {m['owner']} at {m['place']} once you part."
                                        f" Talk to {m['owner']} meanwhile to cover for {first}.")
                else:
                    self.notices.append(f"{first} goes to work on {m['owner']} at {m['place']} once you part.")
                return (f"The player picked {m['owner']} at {m['place']} from {name}'s plan"
                        f"{' and will keep them talking as cover' if cover else ''}. {name} confirms and will set off"
                        f" alone once they part; not before.")
        if job is not None and job.stage == "approved" and offers_cover(text):
            note_cover(db, job, now)
            return f"The player will keep {job.mark['owner']} talking while {name} works."
        if job is not None and job.open and job.stage != "done" and re.search(
                r"\b(?:call\s+it\s+off|forget\s+(?:it|the\s+(?:job|plan))|cancel|don'?t\s+do\s+it)\b", text or "",
                re.IGNORECASE):
            drop(db, job, now)
            self.notices.append(f"{first}'s plan is called off.")
            return f"The player called off {name}'s side job. {name} lets it go."
        kind = player_asks(text)
        if kind is None:
            return None
        if job is not None and job.stage in ("scouting", "approved"):
            return (f"{name} already has a side job going for the player ({job.kind}); {name} reminds them of it"
                    f" instead of starting another.")
        if not in_whiterun(location):
            return (f"The player asked {name} to go {'stealing' if kind == THEFT else 'flirting for coin'}."
                    f" {name} only knows the streets of Whiterun well enough for that, and says it will have to wait"
                    f" until they are back in Whiterun.")
        job, _ = ask(db, me, player, kind, now, location)
        self.brought_up = True
        self.travel[name] = "Whiterun Marketplace"
        self.notices.append(f"{first} will scout Whiterun for marks. Talk to {first} alone in a few hours.")
        what = ("go stealing in Whiterun: jewellery and anything else worth 30 gold or more" if kind == THEFT else
                "flirt with the men and guards of Whiterun for coin")
        return (f"The player asked {name} to {what}. {name} agrees, because {name} has no real choice, but first"
                f" {name} will scout: make connections, watch who has what, and come back to the player with marks to"
                f" approve. {name} does not do the job itself yet and says so in {name}'s own words. When they part,"
                f" {name} heads into town." + why(db, me, name, player, _name(db, player), now))

    def proposal(self, job: Job, name: str) -> None:
        """The game shows her marks as numbered choices, once per conversation."""
        if job.job_id in self.proposed:
            return
        self.proposed.add(job.job_id)
        first = name.split(" ")[0]
        numbers = ("one", "two", "three")[:len(job.marks)]
        self.notices.append(f"{first}'s marks: say {', '.join(numbers[:-1])} or {numbers[-1]}, or no:"
                            if len(numbers) > 1 else f"{first}'s mark: say yes or no:")
        self.notices += [f"{i + 1}: {m['owner']}, {m['place']} ({m['risk']} risk)" for i, m in enumerate(job.marks)]

    def take_notices(self) -> list[str]:
        out, self.notices = self.notices, []
        return out

    def take_orders(self) -> list[str]:
        out, self.orders = self.orders, []
        return [o.encode() for o in out]

    def take_travel(self, name: str, offered: set[str]) -> dict[str, Any] | None:
        place = self.travel.pop(name, None)
        if place is None or TRAVEL_TO not in offered:
            return None
        return {"identifier": TRAVEL_TO, "arguments": {"source": [name], "location": place}}

    def closed(self, db, now: float, spoke: set[int]) -> None:
        """Outcomes she was given to report, in a conversation where she spoke, count as told."""
        for npc_id, job in self.told.items():
            if npc_id in spoke and not job.reported:
                mark_reported(db, job, now)


class TravelWatcher:
    """In the reply parser chain: her line after the player's ask or pick carries TravelTo to the place
    (it starts once the conversation is over)."""

    def __init__(self, desk: Desk, loaded: Any = ()):
        self.desk = desk
        self.offered = set(loaded) & {TRAVEL_TO}

    def note_actions(self, actions: Any) -> None:
        return None

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        action = self.desk.take_travel(speaker_name, self.offered)
        if action is None:
            return []
        if any(isinstance(a, dict) and a.get("identifier") == TRAVEL_TO for a in sentence_actions):
            return []
        sentence_actions.append(action)
        logger.log(23, f"Living World side jobs: {action}")
        return [TRAVEL_TO]


# ---------------------------------------------------------------- game records

def is_job_record(raw: str) -> bool:
    return "|type=job|" in (raw or "").lower() or (raw or "").lower().endswith("|type=job")


def parse_loot(text: str | None) -> list[dict[str, Any]]:
    items = []
    for part in (text or "").split(";"):
        name, _, value = part.partition(":")
        if name.strip():
            try:
                items.append({"name": name.strip(), "value": int(float(value))})
            except ValueError:
                items.append({"name": name.strip(), "value": None})
    return items


def fields_of(raw: str) -> dict[str, str]:
    out = {}
    for part in raw.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v
    return out
