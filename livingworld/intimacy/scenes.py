"""OStim scenes as Living World events: one ``scene`` record per finished scene, and a live snapshot.

A finished scene becomes one ``world_event``:

* ``romance.scene`` for an ordinary scene. Each NPC in it remembers it as something shared; touching,
  grabbing, slaps and rough play during it are part of it, said so in the memory, and change nothing else.
  It adds a little intimacy and affinity between the people in it, never trust or attraction (the same rule
  as NPC romance). Bystanders who saw it remember seeing it, and the social side turns that into a
  ``caught_with`` claim, so a partner who hears of it reacts as they would to any affair.
* ``intimacy.forced`` for a scene OStim played as aggressive. The person it was done to remembers being
  forced, with a large loss of trust; bystanders remember an assault (claim ``forced``).

No LLM: rules and templates only, like the rest of ``perception/``.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src import utils

from livingworld.intimacy import size as sizes
from livingworld.perception import formation, intoxication
from livingworld.perception.formation import Bond, MemoryDraft, Perception
from livingworld.perception.records import PerceptionRecord, Witness

logger = utils.get_logger()

KEY_SCENE = "mantella_lw_scene"                # context: one 'scene' record (via=now) per running scene nearby
KEY_INTIMACY_ON = "mantella_lw_intimacy_on"    # start reply: the game folds in-scene grabs and hits into the scene

EVENT_SCENE = "romance.scene"
EVENT_FORCED = "intimacy.forced"

# OStim action types (SKSE/Plugins/OStim/actions/*.json) in plain words, in the order they read best.
# Holding a hand or an arm, reading, dancing and the like are left out: they are not what anyone remembers.
_ACT_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("kissing", ("kissing", "frenchkissing", "kissingneck", "kissingcheek", "kissinghand", "kissingfoot")),
    ("holding each other", ("hugging", "cuddling", "spooning")),
    ("caressing", ("gropingbreast", "gropingbutt", "gropingtesticles", "massaging", "strokinghead",
                   "lickingnipple", "suckingnipple")),
    ("using hands", ("handjob", "vaginalfingering", "analfingering", "femalemasturbation", "malemasturbation",
                     "boobjob", "footjob", "thighjob", "buttjob", "grindingthigh", "grindingpenis")),
    ("using mouths", ("blowjob", "cunnilingus", "deepthroating", "penilelicking", "anallicking", "rimjob",
                      "testicularlicking", "vaginallicking")),
    ("sex", ("vaginalsex", "analsex", "tribbing", "vaginaltailsex", "analtailsex")),
    ("spanking", ("spanking", "breastslapping")),
    ("rough play", ("choking", "pullinghair")),
]
ROUGH_ACTS = {"spanking", "breastslapping", "choking", "pullinghair"}

IMPORTANCE = 0.75
WITNESS_IMPORTANCE = 0.6
FORCED_IMPORTANCE = 0.95
PARTNER_DELTAS = {"intimacy": 0.06, "affinity": 0.03}
FORCED_VICTIM_DELTAS = {"trust": -0.5, "fear": 0.35, "affinity": -0.4}
FORCED_WITNESS_DELTAS = {"trust": -0.3, "fear": 0.15, "affinity": -0.25}

# How far apart the pieces of one scene can be: live snapshots, climaxes, a scene stored as still running when
# a conversation closed, and the game's end record for it. Same value as ``climax.SAME_SCENE_HOURS``.
SAME_SCENE_HOURS = 12.0


def act_words(acts: list[str]) -> list[str]:
    """['kissing', 'sex', 'spanking'] from OStim action types, each once."""
    seen = set(acts)
    return [words for words, ids in _ACT_WORDS if seen.intersection(ids)]


def _list(words: list[str]) -> str:
    if not words:
        return ""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _place(record: PerceptionRecord) -> str:
    return f" in {record.location}" if record.location else ""


def _how_long(seconds: float | None) -> str:
    minutes = round((seconds or 0.0) / 60.0)
    if minutes < 2:
        return ""
    return f" for about {minutes} minutes"


def _rough(record: PerceptionRecord) -> bool:
    return bool(record.slaps) or bool(ROUGH_ACTS.intersection(record.acts))


def _extras(record: PerceptionRecord) -> str:
    bits = []
    if record.touches >= 3:
        bits.append("hands all over each other")
    if record.slaps:
        bits.append("a slap or two" if record.slaps <= 2 else "plenty of slaps")
    return f" There were {_list(bits)}." if bits else ""


# ---- memories ------------------------------------------------------------------------------------

def shared_for_partner(record: PerceptionRecord, others: list[str], ongoing: bool = False) -> MemoryDraft:
    """An NPC who was in an ordinary scene. ``ongoing``: stored while it still ran (no length yet)."""
    acts = act_words(record.acts)
    what = f": {_list(acts)}" if acts else ""
    interpretation = "Something we shared."
    if _rough(record) or record.touches:
        interpretation += " The grabbing and the slaps were part of it, not violence."
    return MemoryDraft(
        content=f"{_list(others)} and I were intimate{_place(record)}"
                f"{'' if ongoing else _how_long(record.duration_s)}{what}.{_extras(record)}",
        interpretation=interpretation, importance=IMPORTANCE, valence=0.5, intensity=0.6, confidence=1.0,
        source_kind="experienced", deltas_to_actor=dict(PARTNER_DELTAS))


def shared_for_witness(record: PerceptionRecord, names: list[str], seen: Perception, to_any_close: bool) -> MemoryDraft:
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    distance = seen.distance_m or 0.0
    seen_from = "" if distance <= formation.CLEAR_VIEW_M else f", from about {round(distance)} metres away"
    interpretation = "Not something I expected to walk in on." if to_any_close else None
    return MemoryDraft(
        content=f"{hedge} {_list(names)} being intimate{_place(record)}{seen_from}.",
        interpretation=interpretation, importance=formation._clamp(WITNESS_IMPORTANCE * (0.7 + 0.3 * seen.confidence)),
        valence=-0.05, intensity=0.4, confidence=seen.confidence, source_kind="witnessed", deltas_to_actor={})


BETRAYED_TO_PARTNER = {"trust": -0.4, "affinity": -0.3}
BETRAYED_TO_RIVAL = {"affinity": -0.35, "trust": -0.15}


def betrayal_for_witness(record: PerceptionRecord, names: list[str], partner: str, seen: Perception) -> MemoryDraft:
    """A witness who saw their own spouse or partner in a scene with someone else."""
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    others = [n for n in names if n != partner]
    return MemoryDraft(
        content=f"{hedge} {partner} being intimate with {_list(others)}{_place(record)}.",
        interpretation=f"{partner} is mine. I will not forget this.", importance=0.95, valence=-0.85,
        intensity=0.9, confidence=seen.confidence, source_kind="witnessed", deltas_to_actor=dict(BETRAYED_TO_PARTNER))


def forced_for_victim(record: PerceptionRecord, aggressor: str) -> MemoryDraft:
    return MemoryDraft(
        content=f"{aggressor} forced themselves on me{_place(record)}.",
        interpretation=f"I did not want it. {aggressor} is dangerous.", importance=FORCED_IMPORTANCE, valence=-0.9,
        intensity=0.95, confidence=1.0, source_kind="experienced", deltas_to_actor=dict(FORCED_VICTIM_DELTAS))


def forced_for_aggressor(record: PerceptionRecord, victims: list[str]) -> MemoryDraft:
    return MemoryDraft(content=f"I took {_list(victims)}{_place(record)} against their will.", interpretation=None,
                       importance=0.6, valence=0.0, intensity=0.5, confidence=1.0, source_kind="experienced",
                       deltas_to_actor={})


def forced_for_witness(record: PerceptionRecord, aggressor: str, victims: list[str], seen: Perception) -> MemoryDraft:
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    return MemoryDraft(
        content=f"{hedge} {aggressor} force themselves on {_list(victims)}{_place(record)}.",
        interpretation=f"{aggressor} is dangerous.", importance=formation._clamp(0.85 * (0.7 + 0.3 * seen.confidence)),
        valence=-0.7, intensity=0.8, confidence=seen.confidence, source_kind="witnessed",
        deltas_to_actor=dict(FORCED_WITNESS_DELTAS))


# ---- storing a finished scene --------------------------------------------------------------------

def record_scene(pipeline, record: PerceptionRecord, ongoing: bool = False):
    """Store a finished scene (``via=end``). Called by ``PerceptionPipeline.record``.

    ``ongoing``: the scene was still running when the conversation closed (``LivingWorld._close``), so the game's
    end record for it may never reach the server; it is stored now from the last live snapshot, marked
    ``ongoing``. When that end record (or a later close) does arrive for the same thread and people, it completes
    the stored event instead of adding a second one: the acts, counts and length are brought up to date, and only
    people who have no memory of it yet (someone who walked in later) get one."""
    from livingworld.perception.pipeline import Outcome
    store = pipeline.store
    existing = store.scalar(
        "SELECT e.event_id FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id"
        " WHERE e.game_key = ? AND e.game_time <= s.cutoff_gt", (record.key,))
    if existing is not None:
        return Outcome(existing, duplicate=True)
    seen_live = getattr(pipeline, "scene_seers", None)
    if seen_live is not None:
        record = seen_live.merged(record, ending=not ongoing)
    gt = record.game_time
    people: dict[int, str] = {}
    for actor in (record.actor, record.target, record.party):
        if actor is not None:
            people.setdefault(pipeline.identities.ensure_npc(actor.identity(), gt), actor.name)
    if len(people) < 2 or any(pipeline._child(i) for i in people):
        logger.log(23, f"Living World: scene record {record.key} skipped (fewer than two people, or a child)")
        return Outcome(None)
    ids = list(people)
    seers: dict[int, Perception] = {}
    unseen = []
    seer_names = set()
    for witness in record.witnesses:
        npc_id = pipeline.identities.ensure_npc(witness.actor.identity(), gt)
        if npc_id in people or pipeline._child(npc_id):
            continue
        seen = formation.perceives(witness)
        if seen is None:
            unseen.append(witness.actor.name)
        else:
            seer_names.add(witness.actor.name)
            if npc_id not in seers or seen.confidence > seers[npc_id].confidence:
                seers[npc_id] = seen
    unseen = [n for n in dict.fromkeys(unseen) if n not in seer_names]

    timeline = store.active_timeline_id()
    etype = EVENT_FORCED if record.forced else EVENT_SCENE
    payload = {"acts": record.acts, "act_words": act_words(record.acts), "touches": record.touches,
               "slaps": record.slaps, "climax": record.climax, "thread": record.thread, "scene": record.scene_id,
               "place_name": record.location, "forced": record.forced, "player_size": record.player_size, "names": list(people.values()),
               "nearby_but_unseen": unseen}
    if ongoing:
        payload["ongoing"] = True
    else:
        payload["ended_gt"] = gt
    outcome = Outcome(None, unseen=unseen)
    with store.transaction() as db:
        if ongoing and _ended_after(db, timeline, etype, record.thread, ids, gt):
            logger.log(23, f"Living World: scene {record.key} already ended and was stored; not storing it as running")
            return Outcome(None)
        open_event = _open_scene(db, timeline, etype, record.thread, ids, gt)
        remembered: set[int] = set()
        if open_event is not None:
            event_id, old_payload = open_event
            payload = _completed(old_payload, payload, ongoing)
            game_key = None if ongoing else record.key
            db.execute("UPDATE world_event SET payload_json = ?, duration_s = MAX(COALESCE(duration_s, 0), ?),"
                       " game_key = COALESCE(?, game_key) WHERE event_id = ?",
                       (json.dumps(payload), record.duration_s or 0.0, game_key, event_id))
            listed = {r[0] for r in db.execute("SELECT npc_id FROM event_witness WHERE event_id = ?", (event_id,))}
            remembered = {r[0] for r in db.execute("SELECT owner_id FROM memory WHERE event_id = ?", (event_id,))}
        else:
            event_id = db.execute(
                "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
                " game_key, real_time, duration_s, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, etype, record.location, json.dumps(ids), json.dumps(payload), "game", gt,
                 record.key if not ongoing else _open_key(record, ids),
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), record.duration_s, 1.0)).lastrowid
            listed = set()
        outcome.event_id = event_id
        for npc_id in ids:
            if npc_id not in listed:
                db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
                           (event_id, npc_id, "participated"))
            outcome.witnesses[npc_id] = "participated"
        new_seers = []
        for npc_id, seen in seers.items():
            if npc_id not in listed:
                db.execute("INSERT INTO event_witness (event_id, npc_id, how, distance_m, had_los) VALUES (?,?,?,?,?)",
                           (event_id, npc_id, seen.how, seen.distance_m, 1))
                new_seers.append(npc_id)
            outcome.witnesses[npc_id] = seen.how

        drafts, betrayals = _drafts(pipeline, db, record, people, seers, ongoing)
        drafts = {i: d for i, d in drafts.items() if i not in remembered}
        betrayals = [b for b in betrayals if b[0] not in remembered]
        for npc_id, (draft, towards) in drafts.items():
            intoxication.hazy(draft, intoxication.drink_state(db, npc_id, gt).stage)
            about = [i for i in ids if i != npc_id]
            mem_id = db.execute(
                "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                " importance, valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (timeline, npc_id, "episodic", draft.content, json.dumps(about), record.location, gt,
                 draft.importance, draft.valence, draft.intensity, "perception", draft.source_kind,
                 draft.interpretation, draft.confidence, event_id)).lastrowid
            db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
            outcome.memories[npc_id] = mem_id
            if draft.deltas_to_actor:
                for other in towards:
                    if other != npc_id:
                        pipeline._relationship_change(db, timeline, npc_id, other, draft, mem_id, event_id, gt)
        for npc_id, rivals, rival_draft in betrayals:
            mem_id = outcome.memories.get(npc_id)
            for rival in rivals:
                if mem_id is not None and not pipeline._is_player(db, npc_id):
                    pipeline._relationship_change(db, timeline, npc_id, rival, rival_draft, mem_id, event_id, gt)
        if open_event is not None and new_seers:
            from livingworld.social import claims
            claims.witnesses_added(db, event_id, new_seers)   # already talked about: give the newcomers their copy
    how = "still running, " if ongoing else ""
    what = "completed" if open_event is not None else "stored"
    logger.log(23, f"Living World: {what} {etype} event {event_id} ({how}{record.key}): {', '.join(people.values())};"
                   f" acts {', '.join(record.acts) or 'none'}; touches {record.touches}, slaps {record.slaps};"
                   f" seen by {len(seers)}; memories for {len(outcome.memories)} NPC(s)")
    return outcome


def _drafts(pipeline, db, record: PerceptionRecord, people: dict[int, str], seers: dict[int, Perception],
            ongoing: bool):
    """The memory each person gets: npc_id -> (memory, who its deltas are towards), and the betrayals
    (witness, the partner's lovers, their deltas)."""
    ids = list(people)
    aggressor_id = ids[0]                    # the game puts the player first, else OStim's first (dominant) actor
    drafts: dict[int, tuple[MemoryDraft, list[int]]] = {}
    betrayals: list[tuple[int, list[int], MemoryDraft]] = []
    aggressor = people[aggressor_id]
    victims = [n for i, n in people.items() if i != aggressor_id]
    for npc_id, name in people.items():
        if pipeline._is_player(db, npc_id):
            continue                     # the player keeps their own memories
        others = [n for i, n in people.items() if i != npc_id]
        if not record.forced:
            drafts[npc_id] = (shared_for_partner(record, others, ongoing), [i for i in ids if i != npc_id])
        elif npc_id == aggressor_id:
            drafts[npc_id] = (forced_for_aggressor(record, victims), [])
        else:
            drafts[npc_id] = (forced_for_victim(record, aggressor), [aggressor_id])
    for npc_id, seen in seers.items():
        if pipeline._is_player(db, npc_id):
            continue
        if record.forced:
            drafts[npc_id] = (forced_for_witness(record, aggressor, victims, seen), [aggressor_id])
        else:
            from livingworld.intimacy.common import partner_in
            partner = partner_in(db, npc_id, ids)
            if partner is not None:
                # their own spouse or partner with someone else: hurt by the partner, angry at the other
                drafts[npc_id] = (betrayal_for_witness(record, list(people.values()), people[partner], seen), [partner])
                rivals = [i for i in ids if i != partner and i != npc_id]
                rival_draft = MemoryDraft("", f"was intimate with {people[partner]}", 0, 0, 0, 1.0, "witnessed",
                                          dict(BETRAYED_TO_RIVAL))
                betrayals.append((npc_id, rivals, rival_draft))
                continue
            close = any(pipeline._bond(db, npc_id, i).close for i in ids)
            drafts[npc_id] = (shared_for_witness(record, list(people.values()), seen, close), [])

    player_size = sizes.from_scale(record.player_size) if record.actor.is_player and not record.forced else None
    if player_size is not None:
        from livingworld.perception.attire import sex_of
        for npc_id in ids:
            if npc_id in drafts and sex_of(db, npc_id) == "female":
                draft = drafts[npc_id][0]
                draft.content += sizes.memory_line(record.actor.name, player_size)
                draft.valence = round(draft.valence + (0.1 if player_size.impressive else -0.1), 2)
    return drafts, betrayals


def _open_key(record: PerceptionRecord, ids: list[int]) -> str:
    """The game key of a scene stored while still running (the game's snapshot keys are not the end key)."""
    return f"open:{record.thread}:{'-'.join(str(i) for i in sorted(ids))}:{record.game_time:.5f}"


def _open_scene(db, timeline: int, etype: str, thread, ids: list[int], gt: float) -> tuple[int, dict] | None:
    """A scene of the same kind, thread and people stored as still running on this timeline, not long before."""
    rows = db.execute(
        "SELECT event_id, actors_json, payload_json FROM world_event WHERE timeline_id = ? AND type = ?"
        " AND json_extract(payload_json, '$.ongoing') = 1 AND json_extract(payload_json, '$.thread') IS ?"
        " AND game_time >= ? AND game_time <= ? ORDER BY event_id DESC",
        (timeline, etype, thread, gt - SAME_SCENE_HOURS / 24.0, gt + 1e-6)).fetchall()
    for event_id, actors_json, payload_json in rows:
        if set(json.loads(actors_json or "[]")) == set(ids):
            return event_id, json.loads(payload_json or "{}")
    return None


def _ended_after(db, timeline: int, etype: str, thread, ids: list[int], gt: float) -> bool:
    """The game's end record for this scene already came (it ended at or after the snapshot's time)."""
    rows = db.execute(
        "SELECT actors_json FROM world_event WHERE timeline_id = ? AND type = ?"
        " AND COALESCE(json_extract(payload_json, '$.ongoing'), 0) = 0 AND json_extract(payload_json, '$.thread') IS ?"
        " AND COALESCE(json_extract(payload_json, '$.ended_gt'), game_time) >= ? AND game_time <= ?",
        (timeline, etype, thread, gt - 1e-6, gt + SAME_SCENE_HOURS / 24.0)).fetchall()
    return any(set(json.loads(r[0] or "[]")) == set(ids) for r in rows)


def _completed(old: dict, new: dict, ongoing: bool) -> dict:
    """The stored payload of a still-running scene brought up to date by a later snapshot or the end record."""
    out = dict(new)
    out["acts"] = list(dict.fromkeys((old.get("acts") or []) + (new.get("acts") or [])))
    out["act_words"] = act_words(out["acts"])
    for count in ("touches", "slaps", "climax"):
        out[count] = max(old.get(count) or 0, new.get(count) or 0)
    out["nearby_but_unseen"] = list(dict.fromkeys(new.get("nearby_but_unseen") or []))
    out["ongoing"] = bool(ongoing)
    if not ongoing:
        out["stored_while_running"] = True
    return out


# ---- who could see a scene while it ran ---------------------------------------------------------

def _better(view: Witness, than: Witness) -> bool:
    """A view with line of sight beats one without; between equals, the nearer one."""
    if view.had_los != than.had_los:
        return view.had_los
    return view.distance_m < than.distance_m


@dataclass
class _Watch:
    thread: int | None
    people: frozenset[int]                   # game ref ids of the people in it
    first_gt: float
    last_gt: float
    seers: dict[int, Witness] = field(default_factory=dict)   # ref id -> their best view so far
    ended: bool = False


class SceneSeers:
    """Who the live snapshots (``via=now``) said could see each running scene, kept until its end record.

    The game samples witnesses for the end record separately, at the end, from wherever people are then
    (2026-11-11, Bits and Pieces: the snapshots told Sayma and Gerdur they could see Gisli's scene, the end
    record had Gerdur without a view and no Sayma, so only Gisli remembered it). Anyone a snapshot showed with
    a view is added to the end record and to climaxes from the same scene."""

    def __init__(self) -> None:
        self._watches: list[_Watch] = []

    @staticmethod
    def _people(record: PerceptionRecord) -> frozenset[int]:
        return frozenset(a.ref_id for a in (record.actor, record.target, record.party) if a is not None)

    def note(self, records: list[PerceptionRecord]) -> None:
        """Take in one request's snapshot records."""
        for record in records:
            if record.type != "scene" or record.via != "now":
                continue
            people, gt = self._people(record), record.game_time
            watch = next((w for w in reversed(self._watches) if not w.ended and w.thread == record.thread
                          and w.people == people and w.last_gt >= gt - SAME_SCENE_HOURS / 24.0), None)
            if watch is None:
                watch = _Watch(record.thread, people, gt, gt)
                self._watches.append(watch)
            watch.last_gt = max(watch.last_gt, gt)
            for witness in record.witnesses:
                ref = witness.actor.ref_id
                if ref in people:
                    continue
                if ref not in watch.seers or _better(witness, watch.seers[ref]):
                    watch.seers[ref] = witness
        if records:
            newest = max(r.game_time for r in records)
            self._watches = [w for w in self._watches if w.last_gt >= newest - SAME_SCENE_HOURS / 24.0]

    def _find(self, record: PerceptionRecord) -> _Watch | None:
        people, gt = self._people(record), record.game_time
        for watch in reversed(self._watches):
            if watch.ended or watch.thread != record.thread or not people <= watch.people:
                continue
            if record.type == "scene" and people != watch.people:
                continue
            if watch.first_gt <= gt + 1e-6 and watch.last_gt >= gt - SAME_SCENE_HOURS / 24.0:
                return watch
        return None

    def merged(self, record: PerceptionRecord, ending: bool = True) -> PerceptionRecord:
        """``record`` with everyone the snapshots showed seeing its scene added to its witnesses (or their view,
        when the record lists them without one). ``ending``: this is the scene's end record, the watch ends."""
        watch = self._find(record)
        if watch is None:
            return record
        if ending and record.type == "scene":
            watch.ended = True
        witnesses = list(record.witnesses)
        at = {w.actor.ref_id: i for i, w in enumerate(witnesses)}
        added = []
        for ref, view in watch.seers.items():
            if formation.perceives(view) is None:
                continue
            if ref not in at:
                witnesses.append(view)
                added.append(view.actor.name)
            elif formation.perceives(witnesses[at[ref]]) is None:
                witnesses[at[ref]] = view
                added.append(view.actor.name)
        if not added:
            return record
        logger.log(23, f"Living World: {record.type} record {record.key}: the live snapshots also had"
                       f" {', '.join(added)} seeing it")
        return dataclasses.replace(record, witnesses=witnesses)

    def open_records(self, records: list[PerceptionRecord]) -> list[PerceptionRecord]:
        """The last snapshot's scenes as end-like records, with everyone who could see them along the way."""
        out = []
        for record in records:
            if record.type != "scene" or record.via != "now" or record.target is None:
                continue
            out.append(self.merged(dataclasses.replace(record, via="end"), ending=False))
        return out


# ---- what is going on right now ------------------------------------------------------------------

@dataclass
class SceneNow:
    """A scene running right now, from the game's snapshot."""
    people: dict[int, str]                   # npc_id -> name, the player first when they are in it
    acts: list[str]
    forced: bool = False
    seen_by: set[int] = field(default_factory=set)
    player_id: int | None = None             # the player, when they are in it
    player_size: sizes.Size | None = None    # TNG size, when the player is a man in it
    seer_names: dict[int, str] = field(default_factory=dict)   # npc_id -> name of each person who can see it


def snapshot(records: list[PerceptionRecord], ids: dict[int, int]) -> list[SceneNow]:
    """``ids`` maps game ref ids to npc_ids (every actor and witness on the records)."""
    scenes = []
    for record in records:
        if record.type != "scene":
            continue
        people = {}
        for actor in (record.actor, record.target, record.party):
            if actor is not None and actor.ref_id in ids:
                people.setdefault(ids[actor.ref_id], actor.name)
        if len(people) < 2:
            continue
        seers = {ids[w.actor.ref_id]: w.actor.name for w in record.witnesses
                 if w.actor.ref_id in ids and formation.perceives(w) is not None and ids[w.actor.ref_id] not in people}
        player_id = ids.get(record.actor.ref_id) if record.actor.is_player else None
        scenes.append(SceneNow(people, record.acts, record.forced, set(seers), player_id,
                               sizes.from_scale(record.player_size) if player_id is not None else None, seers))
    return scenes


def in_scene(scenes: list[SceneNow], npc_id: int) -> bool:
    return any(npc_id in s.people for s in scenes)


def turn_note(npc_id: int, name: str, scenes: list[SceneNow]) -> str | None:
    """One short line for the player's turn: ``npc_id`` is in, or can see, a scene right now, and answers from
    there instead of carrying on with whatever was being talked about before."""
    for scene in scenes:
        acts = act_words(scene.acts)
        what = f" ({_list(acts)})" if acts else ""
        if npc_id in scene.people:
            others = _list([n for i, n in scene.people.items() if i != npc_id])
            if scene.forced:
                return None              # the prompt lines carry that one; no nudge to "answer from the scene"
            return (f"{name} is in the middle of an intimate scene with {others} as they speak{what}. {name}'s reply"
                    f" comes from inside that moment; earlier topics wait unless {name} chooses to raise them.")
    for scene in scenes:
        if npc_id in scene.seen_by:
            names = _list(list(scene.people.values()))
            if scene.forced:
                return f"{name} can see {names} right in front of them, and it looks like someone is being forced."
            what = f" ({_list(act_words(scene.acts))})" if scene.acts and act_words(scene.acts) else ""
            return (f"{name} can see {names} being intimate right in front of them{what}. {name} does not act as if"
                    f" it were not happening: {name} reacts to it in their own way first.")
    return None


def lines_for(npc_id: int, name: str, scenes: list[SceneNow], present: dict[int, str],
              is_woman: bool = False, partners: set[int] | None = None, confront: bool = False,
              watched_by: set[int] | None = None) -> list[str]:
    """Prompt lines for ``npc_id`` about scenes running right now. ``is_woman``: she reacts to the player's size.
    ``partners``: the people ``npc_id`` is with (spouse, partner, courting), to see a betrayal for what it is.
    ``confront`` (``confront.py``): a partner who sees it speaks up, and ``watched_by`` (the people who are with
    ``npc_id`` and can see their scene) tells the one in the scene who is watching."""
    lines = []
    for scene in scenes:
        acts = act_words(scene.acts)
        what = f" ({_list(acts)})" if acts else ""
        if npc_id in scene.people:
            others = _list([n for i, n in scene.people.items() if i != npc_id])
            if scene.forced:
                first = next(iter(scene.people))
                if first == npc_id:
                    lines.append(f"{name} is forcing {others} into an intimate scene right now.")
                else:
                    lines.append(f"{name} is being forced into an intimate scene by {scene.people[first]} right now"
                                 f" and does not want it.")
                continue
            lines.append(f"{name} is in the middle of an intimate scene with {others} right now{what}; their clothes"
                         f" are off for it.")
            lines.append(f"Touching, grabbing, slaps and rough play between {name} and {others} right now are part of"
                         f" this scene, not an attack or an insult. {name} can still say if something is too much.")
            caught_by = [n for i, n in scene.seer_names.items() if watched_by and i in watched_by]
            if confront and caught_by:
                lines.append(f"{_list(caught_by)}, {name}'s own partner, has walked in and can see this. {name} has"
                             f" been caught: how {name} answers is {name}'s own (ashamed, defiant, pleading, cold).")
            if is_woman and scene.player_size is not None and scene.player_id is not None and scene.player_id != npc_id:
                lines.append(sizes.prompt_line(name, scene.people[scene.player_id], scene.player_size))
        elif npc_id in scene.seen_by:
            names = _list(list(scene.people.values()))
            if scene.forced:
                lines.append(f"{name} can see {names} close by: it looks like someone is being forced.")
            else:
                mine = [n for i, n in scene.people.items() if partners and i in partners]
                if mine:
                    others = _list([n for i, n in scene.people.items() if n not in mine])
                    if confront:
                        lines.append(f"{name} has just walked in on {_list(mine)}, {name}'s own partner, being intimate"
                                     f" with {others} right now{what}. {name} does not stay silent: {name} confronts"
                                     f" them about it now, in {name}'s own way (anger, hurt, demanding answers, or"
                                     f" turning cold and walking out).")
                    else:
                        lines.append(f"{name} can see {_list(mine)}, {name}'s own partner, being intimate with {others}"
                                     f" close by, right now{what}. {name} reacts as {name} would to that.")
                else:
                    lines.append(f"{name} can see {names} being intimate close by{what}.")
    return lines
