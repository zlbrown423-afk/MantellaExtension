"""The bridge between upstream Mantella and the Living World store.

``GameStateManager`` calls these hooks at a few points (each marked ``LIVINGWORLD PATCH``).
The hooks only record; they never change what Mantella sends to the game, and any error inside
them is logged and swallowed so the store can never break a conversation.

Ending a conversation queues a memory-extraction job that a background thread runs, so the game
never waits on it. Switched on with the environment variable ``MANTELLA_LIVING_WORLD=1``. Off by default until
Milestone 1 is complete.
"""
from __future__ import annotations

import functools
import os
import sqlite3
import time
from typing import Any, Callable

from livingworld.memory.extraction import Extractor
from livingworld.memory.jobs import JobQueue, JobWorker
from livingworld.memory import backfill
from livingworld.memory import settings as memory_settings
from livingworld.memory.recall import recall_for
from livingworld import mood as moods
from livingworld.bodylang import body_language_enabled, gesture_notices, gestures_enabled
from livingworld.bodylang.presence import is_gesture_record, is_presence
from livingworld.bodylang.service import BodyLanguage
from livingworld.dialogue import hearing, leads as player_leads
from livingworld.dialogue import joins, place, promises, trips
from livingworld import fog
from livingworld import diary
from livingworld.diary import service as diaries
from livingworld.fog import places as fog_places, service as fog_names
from livingworld.perception import awareness
from livingworld.perception.intoxication import drink_state
from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.social.traits import seed_traits_from_bio
from livingworld.social.world import SocialWorld
from livingworld.work import tasks_enabled
from livingworld.guard import guard_debug_enabled, guard_enabled
from livingworld.intimacy import intimacy_enabled
from livingworld.store.minors import is_child
from livingworld.intimacy import scenes as intimate_scenes
from livingworld.intimacy import confront
from livingworld.guard import decency
from livingworld.social import surrender, wingman
from livingworld.bodylang.peek import KEY_PEEK_ON, peek_enabled
from livingworld.guard.profiles import GuardFacts
from livingworld.guard.service import ACTIONS as GUARD_ACTIONS, ENLIST as GUARD_ENLIST, Guard, Scene as GuardScene
from livingworld.work import words as task_words
from livingworld.work.service import ACTIONS as TASK_ACTIONS, AGREE as TASK_AGREE, Scene as WorkScene, Work
from livingworld.perception.records import KEY_ATTIRE, BadRecord, parse_record, records_from_context
from livingworld.perf.game_records import split_perf_records
from livingworld.perf.monitor import PerfMonitor
from livingworld.perf.stats import STATS as PERF
from livingworld.store.conversation_log import ConversationLog
from livingworld.store.timeline import resolve_load
from livingworld.store.identity import ActorIdentity, FormId, IdentityRegistry, VoiceBindingConflict
from livingworld.store.world_store import WorldStore
from livingworld.store import worlds
from src import utils
from src.http.communication_constants import communication_constants as comm_consts

logger = utils.get_logger()

ENV_VAR = "MANTELLA_LIVING_WORLD"
EXTRACT_JOB = "extract_conversation"

# Sent by the Living World build of Mantella-Spell with each conversation start
KEY_TIMELINE_ID = "mantella_lw_timeline_id"            # timeline stored in the save (also sent back)
KEY_LOAD_NONCE = "mantella_lw_load_nonce"              # random number picked at each game load
KEY_LOADED_GAME_DAYS = "mantella_lw_loaded_game_days"  # game days passed + 1 when the save loaded
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

# Mood meter and quiet vanilla chatter: on with the Living World unless switched off
MOOD_ENV = "MANTELLA_LW_MOOD_METER"
QUIET_ENV = "MANTELLA_LW_QUIET_CHATTER"
HYGIENE_ENV = "MANTELLA_LW_HYGIENE"
HYGIENE_DEBUG_ENV = "MANTELLA_LW_HYGIENE_DEBUG"
KEY_MOOD = "mantella_lw_mood"                    # sent with an NPC line: the meter text to show
KEY_QUIET_CHATTER = "mantella_lw_quiet_chatter"  # sent with the start reply: mute vanilla chatter until the end
KEY_WANTS_A_WORD = "mantella_lw_wants_a_word"    # sent with the start reply: people with work to offer (game side: W2)
KEY_BODY_ON = "mantella_lw_body_on"              # sent with the start reply: body language on or off (game side)
KEY_GESTURES_ON = "mantella_lw_gestures_on"      # sent with the start reply: VR gestures on or off (game side)
KEY_GESTURE_NOTICES = "mantella_lw_gesture_notices"   # start reply: show each recognised gesture (for tuning)
KEY_BODY = "mantella_lw_body"                    # sent with an NPC line: changed expression profiles ('Name|comfort|gaze|guard|step')
KEY_HINTS = "mantella_lw_hints"                  # sent with the first NPC line: what the player overhears (S2)
KEY_GUARD_ISSUE = "mantella_lw_guard_issue"      # sent with an NPC line: items handed to the player ('Name|plugin|0xID;...', for logs)
KEY_GUARD_ISSUE_IDS = "mantella_lw_guard_issue_ids"          # ...the same items as plugin-relative form IDs (the game reads these)
KEY_GUARD_ISSUE_PLUGINS = "mantella_lw_guard_issue_plugins"  # ...and the plugin each one comes from
KEY_GUARD_ISSUE_COUNTS = "mantella_lw_guard_issue_counts"    # ...and how many of each (wages: the gold)
KEY_GUARD_BED_ID = "mantella_lw_guard_bed_id"            # the barracks bunk: plugin-relative REFR id
KEY_GUARD_BED_PLUGIN = "mantella_lw_guard_bed_plugin"
KEY_GUARD_BED_OWN = "mantella_lw_guard_bed_own"          # True: make it the player's; False: unowned again
KEY_GUARD_CHEST_ID = "mantella_lw_guard_chest_id"        # a footlocker (CONT base, plugin-relative id) to place by the bunk
KEY_GUARD_CHEST_PLUGIN = "mantella_lw_guard_chest_plugin"
# G2f arrests: someone the player arrested (by name, as in the conversation) and what the rules decided they do
KEY_GUARD_ARREST_NAME = "mantella_lw_guard_arrest_name"
KEY_GUARD_ARREST_MODE = "mantella_lw_guard_arrest_mode"    # 'submit' (follows to the cells) | 'flee' | 'fight'
KEY_GUARD_ARREST_ID = "mantella_lw_guard_arrest_id"        # int: the arrest (sent back on jail records as task=)
KEY_GUARD_ARREST_DAYS = "mantella_lw_guard_arrest_days"    # float: game days in the cell
KEY_GUARD_JAIL_ID = "mantella_lw_guard_jail_id"            # int: the prisoner marker (REFR, plugin-relative) in the cell
KEY_GUARD_JAIL_PLUGIN = "mantella_lw_guard_jail_plugin"
KEY_GUARD_RELEASE = "mantella_lw_guard_release"            # int[]: arrests whose prisoner is let out now
# G2e shift encounters: the watch windows of the duty in hand (task 0 clears them), sent when they change
KEY_GUARD_WATCH_TASK = "mantella_lw_guard_watch_task"    # int: the duty (task id); 0 = no windows
KEY_GUARD_WATCH_END = "mantella_lw_guard_watch_end"      # float: game days (+1, like the records' gt) the shift ends
KEY_GUARD_WATCH_AT = "mantella_lw_guard_watch_at"        # float[]: when each window falls due
KEY_GUARD_WATCH_WIN = "mantella_lw_guard_watch_win"      # int[]: the window's number (sent back in the watch record)
KEY_GUARD_WATCH_KIND = "mantella_lw_guard_watch_kind"    # int[]: 1 = someone comes over (else notice), 0 = notice
KEY_GUARD_WATCH_OBS = "mantella_lw_guard_watch_obs"      # string[]: what the player notices if nobody comes over
KEY_GUARD_WATCH_COOLDOWN = "mantella_lw_guard_watch_cooldown"   # float: days before the same person comes over again
# Sent by the Living World build of Mantella-Spell (guard careers): per actor, and in the context
KEY_IS_GUARD = "mantella_lw_is_guard"            # actor: Actor.IsGuard()
KEY_CRIME_FACTION = "mantella_lw_crime_faction"  # actor: form ID of Actor.GetCrimeFaction()
KEY_CRIME_PLUGIN = "mantella_lw_crime_plugin"    # actor: the plugin that faction comes from
KEY_PLAYER_BOUNTY = "mantella_lw_player_bounty"  # actor: the player's crime gold in that faction
KEY_THANE_OF = "mantella_lw_thane_of"            # context: holds the player is thane of


def _never_raises(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(self: "LivingWorld", *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return func(self, *args, **kwargs)
        except Exception:
            self.errors += 1
            logger.exception(f"Living World: {func.__name__} failed; Mantella carries on without it")
            return None
    return wrapper


class LivingWorld:
    latest: "LivingWorld | None" = None   # the most recently created instance (used by tests)
    start_workers: bool = True            # tests switch this off to run jobs by hand

    def __init__(self, save_folder: str, enabled: bool = True, default_tts_service: str = "unknown") -> None:
        self.save_folder = save_folder
        self.default_tts_service = default_tts_service
        self.enabled = enabled
        self.errors = 0
        self._stores: dict[str, WorldStore] = {}
        self._world_by_name: dict[str, str] = {}          # name-based world id -> world it was resolved to
        self.store: WorldStore | None = None
        self._identities: dict[str, ActorIdentity] = {}   # Mantella's 6-digit ref id -> full identity
        self._npc_ids: dict[str, int] = {}                # Mantella's 6-digit ref id -> npc_id
        self._present: set[int] = set()
        self.conv_id: int | None = None
        self._context: Any = None
        self._pending = False
        self._workers: dict[str, JobWorker] = {}
        self.complete: Callable[[str, str], str | None] | None = None
        self._last_recall: dict[int, tuple[list[int], list[int]]] = {}  # npc_id -> (know_ids, mem_ids) in their last prompt
        self._rehearsed: set[tuple[int, int]] = set()   # (conv_id, npc_id) already counted as recalled
        self._attire_now: dict[int, awareness.AttireNow] = {}   # latest attire snapshot from the game, by npc_id
        self.mood_meter = enabled and os.environ.get(MOOD_ENV, "").strip().lower() not in _OFF
        self.quiet_chatter = enabled and os.environ.get(QUIET_ENV, "").strip().lower() not in _OFF
        self.hygiene = enabled and os.environ.get(HYGIENE_ENV, "").strip().lower() not in _OFF
        self.hygiene_debug = self.hygiene and os.environ.get(HYGIENE_DEBUG_ENV, "").strip().lower() not in _OFF
        self._moods_shown: dict[int, str] = {}      # npc_id -> meter text the game last showed
        self._moods_to_send: dict[str, str] = {}    # speaker ref id -> meter text to send with their next line
        self._social: dict[int, SocialWorld] = {}   # id(store) -> reputation and gossip for that world
        self._social_refreshed: int | None = None   # conv_id the social world was last brought up to date for
        self._hints_to_send: list[str] = []         # overheard hints for the next NPC line (social/hints.py)
        self._after_load = False                     # the first conversation since a game load is still to come
        self.tasks = enabled and tasks_enabled()    # NPC tasks and shared work (livingworld/work)
        self._work: dict[int, Work] = {}            # id(store) -> tasks for that world
        self._work_refreshed: int | None = None     # conv_id tasks were last brought up to date for
        self.task_words = self.tasks and task_words.words_enabled()   # "I'll help you" / "Thank you!" agrees (MANTELLA_LW_TASK_WORDS=0 off)
        self._help_offered = False                  # the player's last line offered help with the work
        self._npc_last: str | None = None           # the last NPC sentence (did it ask for help?)
        self.guard = enabled and guard_enabled()    # guard careers (livingworld/guard)
        self.guard_debug = enabled and guard_debug_enabled()
        self._guard: dict[int, Guard] = {}          # id(store) -> guard careers for that world
        self._guard_facts: dict[str, GuardFacts] = {}   # Mantella's 6-digit ref id -> what the game said
        self._thane_of: tuple[str, ...] = ()        # holds the game says the player is thane of
        self._guard_sent: set[int] = set()          # id(sentence) whose guard actions were handled
        self._guard_refreshed: int | None = None    # conv_id whose duties/probation were brought up to date
        self._player_said: str | None = None        # the player's last line (kept with a duty report)
        self._watch_sent: tuple | None = None       # G2e: the watch plan last sent to the game this conversation
        self.keep_promises = enabled and promises.promises_enabled()   # NPCs do what they say (MANTELLA_LW_KEEP_PROMISES=0 turns off)
        self.trips = enabled and trips.trips_enabled()   # "let's go to X" sets off after the player's yes (MANTELLA_LW_TRIPS=0 off)
        self._trips: trips.TripPlanner | None = None   # this conversation's trip question and confirmed trip
        self.hearing = enabled and hearing.hearing_enabled()   # catch misheard speech (MANTELLA_LW_HEARING=0 turns off)
        self.leads = enabled and player_leads.leads_enabled()  # ask about a lead without its name (MANTELLA_LW_LEADS=0 turns off)
        self._lead_ask: str | None = None           # the player's last line, while it asks after someone
        self._lead_learned: set[str] = set()        # names learned from that line and its answers
        self.perf_monitor: PerfMonitor | None = None  # MANTELLA_LW_PERF=1 (see livingworld/perf)
        self.body_language = enabled and body_language_enabled()   # MANTELLA_LW_BODY_LANGUAGE=0 turns off
        self._body: dict[int, BodyLanguage] = {}    # id(store) -> body language for that world
        self.intimacy = enabled and intimacy_enabled()   # OStim scenes (livingworld/intimacy); MANTELLA_LW_INTIMACY=0 off
        self._scenes_now: list[intimate_scenes.SceneNow] = []   # scenes running near the player, from the game's snapshot
        self._scene_records: list[Any] = []         # that snapshot's records, stored if still running at close
        self._scene_seers = intimate_scenes.SceneSeers()   # who each running scene's snapshots showed seeing it
        self.confront = self.intimacy and confront.enabled()   # partners who catch each other speak up; MANTELLA_LW_CONFRONT=0 off
        self._fine_sent: set[int] = set()           # lines whose PayFine was handled
        self._held_actions: dict[str, list[dict[str, Any]]] = {}   # speaker ref id -> actions from an action-only reply
        self.asked_joins = enabled and joins.joins_enabled()   # only add people the player asked for (MANTELLA_LW_ASKED_JOINS=0 off)
        self.place = enabled and place.place_enabled()   # tell NPCs whether they are outdoors or inside (MANTELLA_LW_PLACE=0 off)
        self.decency = enabled and decency.enabled()   # guards tell a naked player to cover up; MANTELLA_LW_DECENCY=0 off
        self.surrender = enabled and surrender.enabled()   # a survivor begs for their life; MANTELLA_LW_SURRENDER=0 off
        self.wingman = self.surrender and wingman.enabled()   # ...and plays the player's wingman; MANTELLA_LW_WINGMAN=0 off
        self.peek = enabled and peek_enabled()   # women's eyes drift to a naked player; MANTELLA_LW_PEEK=0 off
        self.fog = enabled and fog.fog_enabled()    # names and places unknown until told (livingworld/fog); MANTELLA_LW_FOG=0 off
        self._fog_labels: dict[str, str | None] = {}   # Mantella's 6-digit ref id -> what the game shows while unknown
        self._fog_reveal: list[str] = []            # real names the player learned, for the next NPC line
        self._fog_notes: list[str] = []             # names learned since the player last spoke, for the dialogue model
        self._fog_game = False                      # the game build hides names (it sends the player's fog label)
        self.diary = enabled and diary.diary_enabled()   # private diaries (livingworld/diary); MANTELLA_LW_DIARY=0 off
        self._ref_raw: dict[str, int] = {}          # Mantella's 6-digit ref id -> the form ID exactly as the game sent it
        self._bios: dict[int, str] = {}             # npc_id -> Mantella bio, for the diary writer
        LivingWorld.latest = self

    @classmethod
    def from_environment(cls, config: Any) -> "LivingWorld":
        enabled = os.environ.get(ENV_VAR, "").strip().lower() in _ON
        if enabled:
            logger.log(23, f"Living World store is on ({ENV_VAR}).")
        tts = getattr(config, "tts_service", None)
        world = cls(config.save_folder, enabled, str(getattr(tts, "value", tts) or "unknown"))
        try:   # VR performance monitor (MANTELLA_LW_PERF=1): watches only, works with the store on or off
            world.perf_monitor = PerfMonitor.from_environment(config.save_folder)
            if world.perf_monitor and cls.start_workers:
                world.perf_monitor.start()
        except Exception:
            logger.exception("Living World: the performance monitor could not start; carrying on without it")
            world.perf_monitor = None
        return world

    def attach_llm(self, client: Any, config: Any) -> None:
        """Use Mantella's summary client (or main client) for background extraction calls."""
        from src.llm.message_thread import message_thread
        from src.llm.messages import UserMessage

        class VerbatimUserMessage(UserMessage):
            # Mantella's UserMessage collapses newlines; the transcript needs one line per utterance
            def get_formatted_content(self) -> str:
                return self.text

        def complete(system: str, user: str) -> str | None:
            messages = message_thread(config, system)
            messages.add_message(VerbatimUserMessage(config, user))
            background = getattr(client, "background_request_call", None)
            if background is not None and memory_settings.memory_boost_enabled():
                # its own reply length and JSON mode: the spoken-reply cap (often 250 tokens) cut extraction off
                return background(messages, {"max_tokens": memory_settings.extract_max_tokens(), "temperature": 0.2},
                                  json_mode=True)
            return client.request_call(messages)
        self.complete = complete

    def wrap_memory(self, inner: Any) -> Any:
        """Wrap Mantella's memory so each NPC's own recalled rows are added to the prompt."""
        if not self.enabled:
            return inner
        from livingworld.memory.remembering import LivingWorldMemory
        return LivingWorldMemory(inner, self)

    # ---- hooks ----
    @_never_raises
    def actors_seen(self, actor_jsons: list[dict[str, Any]] | None) -> None:
        """Keep the full form IDs from the request, before Mantella truncates them."""
        for actor_json in actor_jsons or []:
            # the game has been seen to slip a non-actor entry into the list (an end-conversation request); skip
            # it rather than drop every actor after it
            if not isinstance(actor_json, dict) or comm_consts.KEY_ACTOR_BASEID not in actor_json \
                    or comm_consts.KEY_ACTOR_REFID not in actor_json or comm_consts.KEY_ACTOR_NAME not in actor_json:
                logger.debug(f"Living World: skipped an actor entry with no form IDs: {str(actor_json)[:200]}")
                continue
            identity = ActorIdentity.from_actor_json(actor_json)
            self._identities[identity.ref.mantella_id] = identity
            try:
                self._ref_raw[identity.ref.mantella_id] = int(actor_json[comm_consts.KEY_ACTOR_REFID])
            except (KeyError, TypeError, ValueError):
                pass
            if self.guard and KEY_IS_GUARD in actor_json:
                faction = actor_json.get(KEY_CRIME_FACTION)
                bounty = actor_json.get(KEY_PLAYER_BOUNTY)
                self._guard_facts[identity.ref.mantella_id] = GuardFacts(
                    bool(actor_json.get(KEY_IS_GUARD)), actor_json.get(KEY_CRIME_PLUGIN) or None,
                    f"0x{int(faction) & 0xFFFFFF:08X}" if faction else None,
                    int(bounty) if bounty is not None else None)
            if fog.KEY_FOG_LABEL in actor_json:
                if identity.is_player:
                    self._fog_game = True
                self._fog_labels[identity.ref.mantella_id] = str(actor_json.get(fog.KEY_FOG_LABEL) or "").strip() or None

    @_never_raises
    def perceived(self, context_json: dict[str, Any] | None) -> int:
        """Store the perception records the game queued since its last request (see ``perception/``).
        Runs before Mantella reads the rest of the request, so the first prompt already holds the new memories."""
        started = time.perf_counter()
        if self.guard and isinstance(context_json, dict) and KEY_THANE_OF in context_json:
            self._thane_of = tuple(str(h) for h in (context_json.get(KEY_THANE_OF) or []) if h)
        raw, perf_markers = split_perf_records(records_from_context(context_json), context_json)
        for marker in perf_markers:   # the game's own timing summaries go to the performance monitor only
            PERF.add_marker(marker)
        attire_raw = records_from_context(context_json, KEY_ATTIRE)
        if self.intimacy and self.store is not None and isinstance(context_json, dict) \
                and intimate_scenes.KEY_SCENE in context_json:
            self._scenes_now = self._scene_snapshot(records_from_context(context_json, intimate_scenes.KEY_SCENE))
        body_raw = [r for r in raw if is_presence(r)]   # body language records never reach the event pipeline
        if not gestures_enabled():   # MANTELLA_LW_GESTURES=0: a game still sending them is ignored
            body_raw = [r for r in body_raw if not is_gesture_record(r)]
        raw = [r for r in raw if not is_presence(r)]
        if body_raw and self.store is not None and self.body_language:
            try:
                self._body_for(self.store).ingest(body_raw)
            except Exception:
                self.errors += 1
                logger.exception("Living World: body language records failed; carrying on without them")
        if not (raw or attire_raw) or self.store is None:
            return 0
        pipeline = PerceptionPipeline(self.store)
        if self.intimacy:
            pipeline.scene_seers = self._scene_seers   # end and climax records also get the snapshots' seers
        outcomes = pipeline.ingest(raw + attire_raw)
        stored = len([o for o in outcomes if not o.duplicate])
        if stored:
            self._social_for(self.store).appraise_new()   # what was seen becomes something people can pass on
        if attire_raw:
            self._attire_now = self._attire_snapshot(attire_raw, pipeline)
        kinds: dict[str, int] = {}
        for rec in raw:
            kind = rec.lower().split("|type=", 1)[-1].split("|", 1)[0] if "|type=" in rec.lower() else "?"
            kinds[kind] = kinds.get(kind, 0) + 1
        if attire_raw:
            kinds["attire_snapshot"] = len(attire_raw)
        PERF.records_ingested(kinds, (time.perf_counter() - started) * 1000)
        return stored

    def _scene_snapshot(self, scene_raw: list[str]) -> list[intimate_scenes.SceneNow]:
        """OStim scenes running near the player right now (an empty list when the game says none are)."""
        identities = IdentityRegistry(self.store)
        records, ids = [], {}
        for raw in scene_raw:
            try:
                record = parse_record(raw)
            except (BadRecord, ValueError) as e:
                logger.error(f"Living World: scene snapshot skipped ({e}): {raw[:200]}")
                continue
            records.append(record)
            for actor in [record.actor, record.target, record.party] + [w.actor for w in record.witnesses]:
                if actor is not None and actor.ref_id not in ids:
                    ids[actor.ref_id] = identities.ensure_npc(actor.identity(), record.game_time)
        self._scene_records = records
        self._scene_seers.note(records)
        return intimate_scenes.snapshot(records, ids)

    def _attire_snapshot(self, attire_raw: list[str], pipeline: PerceptionPipeline) -> dict[int, awareness.AttireNow]:
        records = []
        for raw in attire_raw:
            try:
                records.append(parse_record(raw))
            except (BadRecord, ValueError):
                continue   # already logged by the pipeline
        ids: dict[int, int] = {}
        for record in records:
            for actor in [record.actor] + [w.actor for w in record.witnesses]:
                if actor.ref_id not in ids:
                    ids[actor.ref_id] = pipeline.identities.ensure_npc(actor.identity(), record.game_time)
        return awareness.snapshot(records, ids, ids)

    def resolve_world(self, name_world: str, request: dict[str, Any]) -> str:
        """The world (database and Mantella summary folder) this conversation belongs to: the world key
        the save carries, so renaming the character keeps the same memories (see ``store/worlds.py``).
        Falls back to Mantella's name-based world when the Living World is off or anything goes wrong."""
        if not self.enabled:
            return name_world
        try:
            world = worlds.resolve_world(self.save_folder, name_world, request)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not work out the world key; using the player's name")
            return name_world
        if world != name_world:
            logger.log(23, f"Living World: '{name_world}' is kept in world '{world}' (same playthrough)")
        self._world_by_name[name_world] = world
        return world

    def world_for_name(self, name_world: str) -> str:
        """The world a name-based id was last resolved to this session (diary requests send the name)."""
        return self._world_by_name.get(name_world, name_world)

    @_never_raises
    def game_loaded(self, world_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Handle the save load the game reports with each conversation start (see ``store/timeline.py``).
        Returns extra reply keys: the world and the timeline id the game should keep in its save."""
        reply = {worlds.KEY_WORLD_KEY: world_id}
        nonce = request.get(KEY_LOAD_NONCE)
        if nonce is None or request.get(KEY_LOADED_GAME_DAYS) is None:
            return reply
        store = self._store_for(world_id)
        save_timeline = request.get(KEY_TIMELINE_ID) or None
        result = resolve_load(store, int(nonce), int(save_timeline) if save_timeline else None,
                              float(request[KEY_LOADED_GAME_DAYS]))
        if not result.repeated:
            self._after_load = True
            if result.branched:
                logger.log(23, f"Living World: an older save was loaded (game day {request[KEY_LOADED_GAME_DAYS]:.2f}); "
                               f"NPCs now forget everything after it (timeline {result.timeline_id}).")
            else:
                logger.log(23, f"Living World: save loaded on timeline {result.timeline_id}.")
        return {**reply, KEY_TIMELINE_ID: result.timeline_id}

    @_never_raises
    def conversation_starting(self, world_id: str, context: Any) -> None:
        """Called before Mantella reads the start request. Mantella builds the first prompt while it
        reads the request, so the conversation row is opened on first need (see ``_ensure_open``)."""
        if self.conv_id is not None:
            self._close(None)
        self.store = self._store_for(world_id)
        self._context = context
        self._pending = True
        self._attire_now = {}
        self._scenes_now = []
        self._scene_records = []
        self._moods_shown = {}
        self._moods_to_send = {}
        self._guard_sent = set()
        self._held_actions = {}
        self._player_said = None
        self._help_offered = False
        self._npc_last = None
        self._hints_to_send = []
        self._watch_sent = None
        self._lead_ask = None
        self._lead_learned = set()
        self._trips = trips.TripPlanner(here=str(getattr(context, "location", "") or "")) if self.trips else None

    @_never_raises
    def conversation_started(self, world_id: str, context: Any) -> None:
        if context is not self._context:
            self.conversation_starting(world_id, context)
        self._ensure_open()

    def _ensure_open(self) -> None:
        if not self._pending:
            return
        self._pending = False
        context = self._context
        npcs = context.npcs_in_conversation
        kind = "player" if npcs.contains_player_character() else "radiant"
        self.conv_id = ConversationLog(self.store).open(context.location, kind, context.game_days)
        self._npc_ids.clear()
        self._present.clear()
        self._sync(context)
        logger.log(23, f"Living World: conversation {self.conv_id} started in {context.location}"
                       f" at game day {context.game_days:.2f} with {', '.join(npcs.get_all_names())}")

    @_never_raises
    def participants_changed(self, context: Any) -> None:
        if self.conv_id is not None and context is self._context:
            self._sync(context)

    @_never_raises
    def npc_line(self, sentence: Any, context: Any) -> None:
        if self.conv_id is not None and getattr(sentence, "actions", None):
            self._task_actions(sentence, context)
        if self.conv_id is None or sentence.is_narration or sentence.is_system_generated_sentence:
            return
        text = (sentence.text or "").strip()
        if not text:
            return
        speaker = sentence.speaker
        npc_id = self._npc_ids.get(speaker.ref_id) or self._register(speaker, context.game_days)
        try:
            ConversationLog(self.store).append(self.conv_id, npc_id, text, speaker.tts_voice_model, context.game_days)
        except sqlite3.IntegrityError as e:
            # the schema refuses a line whose voice differs from the speaker's bound voice
            self.errors += 1
            logger.error(f"Living World: line by {speaker.name} not stored: {e}")
        if self.leads and self._lead_ask:
            skip = [c.name for c in context.npcs_in_conversation.get_all_characters()]
            self._learn_leads(player_leads.from_reply(self._lead_ask, text, skip), context.game_days)
        if self.task_words and context is self._context:
            self._agree_in_words(sentence, npc_id, text)
        self._npc_last = text

    @_never_raises
    def player_line(self, text: str, context: Any) -> None:
        if self.conv_id is None or not text or not text.strip():
            return
        player = context.npcs_in_conversation.get_player_character()
        if not player:
            return
        npc_id = self._npc_ids.get(player.ref_id) or self._register(player, context.game_days)
        ConversationLog(self.store).append(self.conv_id, npc_id, text.strip(), None, context.game_days)
        self._player_said = text.strip()
        if self.task_words:
            self._help_offered = task_words.player_offers(text, self._npc_last)
        if self.leads:
            self._lead_ask = None
            self._lead_learned = set()
            if hearing.is_question(text) and not player_leads.shortcut(text):
                self._lead_ask = text.strip()
                names = [c.name for c in context.npcs_in_conversation.get_all_characters() if not c.is_player_character]
                self._learn_leads(player_leads.from_player(text, names), context.game_days)
        if self.hygiene_debug:
            self._hygiene_debug(text, npc_id, context)
        if self.guard:
            scene = self._guard_scene()
            if scene is not None and self._guard_for(self.store).player_spoke(scene):
                logger.log(23, "Living World guard: the player answered someone who came over on shift")
        if self.guard_debug:
            scene = self._guard_scene()
            result = self._guard_for(self.store).debug(text, scene) if scene else None
            if result:
                logger.log(23, f"Living World guard test command: {result}")

    def _hygiene_debug(self, text: str, player_id: int, context: Any) -> None:
        from livingworld.hygiene import debug
        command = debug.parse(text)
        if command is None:
            return
        present = {self._npc_ids.get(c.ref_id) or self._register(c, context.game_days): c.name
                   for c in context.npcs_in_conversation.get_all_characters()}
        who = debug.target(text, present, player_id)
        with self.store.transaction() as db:
            debug.apply(db, command, who, context.game_days)
        logger.log(23, f"Living World: hygiene test command '{command}' applied to {present.get(who, 'the player')}")

    @_never_raises
    def conversation_ended(self, game_days: float | None, context: Any | None = None) -> None:
        if self.conv_id is None:
            return
        if game_days is None and context is not None:
            game_days = context.game_days
        if self.guard and self.store is not None:
            try:
                scene = self._guard_scene()
                if scene is not None:
                    self._guard_for(self.store).ended(scene)
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not file the written guard report; carrying on")
        self._close(game_days)

    @_never_raises
    def recall_text(self, characters: list[Any], world_id: str) -> str:
        """The recalled facts and memories for each NPC, each block holding only that NPC's rows."""
        if self._context is None or self._store_for(world_id) is not self.store:
            return ""
        self._ensure_open()
        store = self.store
        now = self._context.game_days
        player = self._context.npcs_in_conversation.get_player_character()
        player_id = (self._npc_ids.get(player.ref_id) or self._register(player, now)) if player else None
        present = {}
        for c in self._context.npcs_in_conversation.get_all_characters():
            present[self._npc_ids.get(c.ref_id) or self._register(c, now)] = c.name
        self._refresh_social(now, list(present), player_id)
        self._refresh_guard()   # before work upkeep, which would expire duties without the reprimand
        self._refresh_work(now)
        scene = self._scene(player_id, player.name if player else "the player", list(present))
        blocks = []
        for character in characters:
            if character.is_player_character:
                continue
            npc_id = self._npc_ids.get(character.ref_id) or self._register(character, now)
            key = (self.conv_id, npc_id)
            recall = recall_for(store, npc_id, now, player_id, mark_rehearsed=key not in self._rehearsed,
                                about_ids=list(present), current_conv_id=self.conv_id)
            self._rehearsed.add(key)
            self._last_recall[npc_id] = (recall.know_ids, recall.mem_ids)
            text = recall.text()
            fog_lines: list[str] = []
            if self._fog_active() and player is not None and player_id is not None:
                player_p = fog_names.Person(player_id, player.name)
                me = self._fog_person(character)
                with store.reading() as db:
                    text = fog_names.hide_player_name(db, npc_id, player_p, text)
                    fog_lines = fog_names.prompt_lines(db, me, player_p)
            if text:
                blocks.append(text)
            if fog_lines:
                blocks.append(f"Names, for {character.name}:\n" + "\n".join(f"- {line}" for line in fog_lines))
            noticed = self._noticed(npc_id, character.name, now, present, player_id,
                                    player.name if player is not None else None)
            if noticed:
                blocks.append(f"What {character.name} notices right now:\n" + "\n".join(f"- {line}" for line in noticed))
            body = self._body_lines(npc_id, character.name, now)
            if body:
                who = player.name if player is not None else "the player"
                blocks.append(f"What {character.name} noticed {who} doing just now (the game saw it happen; "
                              f"mention it only if it matters to {character.name}):\n"
                              + "\n".join(f"- {line}" for line in body))
            if self.mood_meter and player is not None:
                blocks.append("\n".join(moods.prompt_lines(character.name, self._carried_mood(npc_id, now))))
            work = self._work_lines(npc_id, character.name, scene)
            if work:
                blocks.append(f"Work on {character.name}'s mind:\n" + "\n".join(f"- {line}" for line in work))
            if self.diary:
                with store.reading() as db:
                    keeps_diary = diaries.is_keeper(db, npc_id, player_id)
                if keeps_diary:
                    blocks.append(f"{character.name} keeps a private diary and writes in it most evenings, about "
                                  f"their days and the people in them. They carry it or leave it at home, and "
                                  f"would be upset to find anyone had read it.")
            guard = self._guard_lines(npc_id, character.name)
            if guard:
                blocks.append(f"The guard, and {character.name}:\n" + "\n".join(f"- {line}" for line in guard))
        if self.place and player is not None:
            npcs = [c.name for c in characters if not c.is_player_character]
            where = place.prompt_lines(getattr(self._context, "location", ""),
                                       bool(getattr(self._context, "_Context__weather", "")))
            if npcs and where:
                who = npcs[0] if len(npcs) == 1 else "everyone here"
                blocks.append(f"Where {who} is right now:\n" + "\n".join(f"- {line}" for line in where))
        if self.hearing and player is not None:
            npcs = [c.name for c in characters if not c.is_player_character]
            if npcs:
                rule = hearing.prompt_lines(npcs, player.name)
                blocks.append(f"Hearing {player.name}:\n" + "\n".join(f"- {line}" for line in rule))
        return "\n\n".join(blocks)

    @_never_raises
    def guard_notes(self, text: str, context: Any) -> list[str]:
        """G2f: notes for the AI on the player's line, such as what someone the player arrests does."""
        if not self.guard or self.conv_id is None or context is not self._context or not text:
            return []
        scene = self._guard_scene()
        return self._guard_for(self.store).arrest_turn(scene, text) if scene else []

    @_never_raises
    def hearing_note(self, text: str, context: Any) -> str | None:
        """A narration note naming likely misheard words in the player's line, and who they mean by
        "the man I'm looking for" (leads), or None."""
        if context is None or not (self.hearing or self.leads):
            return None
        player = context.npcs_in_conversation.get_player_character()
        player_name = player.name if player else "The player"
        names = [c.name for c in context.npcs_in_conversation.get_all_characters() if not c.is_player_character]
        current = self._current_leads() if self.leads else []
        notes = []
        if self.hearing:
            notes.append(hearing.note_text(text, names, player_name, [l.name for l in current]))
        if self.leads:
            notes.append(player_leads.shortcut_note(text, current, player_name))
        note = " ".join(n for n in notes if n) or None
        if note:
            logger.log(23, f"Living World: {note}")
        return note

    def _current_leads(self) -> list[player_leads.Lead]:
        if self.store is None:
            return []
        with self.store.reading() as db:
            return player_leads.current(db)

    def _learn_leads(self, learned: list[player_leads.Learned], game_time: float) -> None:
        fresh = [l for l in learned if l.name.lower() not in self._lead_learned]
        if not fresh or self.store is None:
            return
        with self.store.transaction() as db:
            for lead in fresh:
                player_leads.remember(db, lead, game_time, self.conv_id)
                self._lead_learned.add(lead.name.lower())
                heard = f' (heard as "{lead.heard_as}")' if lead.heard_as else ""
                logger.log(23, f"Living World: new lead, {lead.kind} '{lead.name}'{heard}")

    def _social_for(self, store: WorldStore) -> SocialWorld:
        if id(store) not in self._social:
            self._social[id(store)] = SocialWorld(store)
        return self._social[id(store)]

    def _refresh_social(self, now: float, present: list[int], player_id: int | None = None) -> None:
        """Once per conversation, before the first prompt: seed, appraise and catch gossip up."""
        if self._social_refreshed == self.conv_id:
            return
        self._social_refreshed = self.conv_id
        try:
            result = self._social_for(self.store).refresh(now, present, player_id=player_id,
                                                          after_load=self._after_load)
            self._after_load = False
            self._hints_to_send = list(result.hints)
            if result.hints:
                logger.log(23, f"Living World hints: {' | '.join(result.hints)}")
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not bring gossip up to date; carrying on without it")

    # ---- tasks and shared work (livingworld/work) ----
    def _work_for(self, store: WorldStore) -> Work:
        if id(store) not in self._work:
            self._work[id(store)] = Work(store)
        return self._work[id(store)]

    def _scene(self, player_id: int | None, player_name: str, present: list[int]) -> WorkScene:
        context = self._context
        hour = getattr(context, "ingame_time", None)
        weather = getattr(context, "_Context__weather", None)   # Mantella keeps it private; read only
        return WorkScene(context.game_days, int(hour) if hour is not None else int((context.game_days % 1) * 24),
                         weather or None, player_id, player_name, present)

    def _refresh_work(self, now: float) -> None:
        """Once per conversation, before the first prompt: open needs, expire deadlines, pause outdoor work."""
        if not self.tasks or self._work_refreshed == self.conv_id:
            return
        self._work_refreshed = self.conv_id
        try:
            scene = self._scene(None, "", [])
            result = self._work_for(self.store).refresh(now, scene.hour, scene.weather)
            if result.opened or result.expired or result.lapsed:
                logger.log(23, f"Living World work: opened {result.opened}, expired {result.expired}, "
                               f"lapsed {result.lapsed}")
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not bring tasks up to date; carrying on without them")

    def _work_lines(self, npc_id: int, name: str, scene: WorkScene) -> list[str]:
        if not self.tasks:
            return []
        try:
            return self._work_for(self.store).lines_for(npc_id, name, scene)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not add task lines; leaving them out")
            return []

    def _player_scene(self) -> WorkScene | None:
        context = self._context
        if context is None:
            return None
        now = context.game_days
        player = context.npcs_in_conversation.get_player_character()
        player_id = (self._npc_ids.get(player.ref_id) or self._register(player, now)) if player else None
        present = [self._npc_ids.get(c.ref_id) or self._register(c, now)
                   for c in context.npcs_in_conversation.get_all_characters()]
        return self._scene(player_id, player.name if player else "the player", present)

    def _task_actions(self, sentence: Any, context: Any) -> None:
        if not self.tasks or context is not self._context:
            return
        wanted = [a for a in sentence.actions if isinstance(a, dict) and a.get("identifier") in TASK_ACTIONS]
        if not wanted:
            return
        scene = self._player_scene()
        for action in wanted:
            # the giver: the NPC named in the call (advanced actions), else the speaker
            source = (action.get("arguments") or {}).get("source")
            name = source[0] if isinstance(source, list) and source else source
            who = next((c for c in context.npcs_in_conversation.get_all_characters()
                        if name and c.name.lower() == str(name).lower()), sentence.speaker)
            if who.is_player_character:
                continue
            npc_id = self._npc_ids.get(who.ref_id) or self._register(who, scene.now)
            result = self._work_for(self.store).action(action["identifier"], npc_id, scene)
            logger.log(23, f"Living World work: {who.name} {action['identifier']}: {result}")

    def _agree_in_words(self, sentence: Any, npc_id: int, text: str) -> None:
        """The player offered help and the NPC took it up in words only: record the task as agreed."""
        if not self._help_offered:
            return
        self._help_offered = False          # only the first NPC sentence after the offer answers it
        if any(isinstance(a, dict) and a.get("identifier") == TASK_AGREE for a in (sentence.actions or [])):
            return                          # the AI called AgreeTask itself
        if not task_words.npc_takes_it_up(text):
            return
        try:
            work = self._work_for(self.store)
            scene = self._player_scene()
            task = work.offered_here(npc_id, scene.now if scene else None)
            if task is None:
                return
            result = work.action(TASK_AGREE, npc_id, scene) if scene else None
            logger.log(23, f"Living World work: {sentence.speaker.name} took up the player's offer in words: {result}")
            if result and "accepted" in result:
                self._hints_to_send.append(f"Agreed with {sentence.speaker.name.split(' ')[0]}: {task.title}.")
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not record a task agreed in words; carrying on")

    # ---- guard careers (livingworld/guard) ----
    def _guard_for(self, store: WorldStore) -> Guard:
        if id(store) not in self._guard:
            self._guard[id(store)] = Guard(store)
        return self._guard[id(store)]

    def _guard_scene(self) -> GuardScene | None:
        scene = self._player_scene()
        if scene is None:
            return None
        facts = {}
        for c in self._context.npcs_in_conversation.get_all_characters():
            f = self._guard_facts.get(c.ref_id)
            if f is not None:
                facts[self._npc_ids.get(c.ref_id) or self._register(c, scene.now)] = f
        return GuardScene(scene.now, scene.player_id, scene.player_name, scene.present,
                          getattr(self._context, "location", None), facts, self._thane_of, self._player_said)

    def _refresh_guard(self) -> None:
        """Once per conversation: missed duties, probation, and whether the player is at their post."""
        if not self.guard or self._guard_refreshed == self.conv_id:
            return
        self._guard_refreshed = self.conv_id
        try:
            scene = self._guard_scene()
            result = self._guard_for(self.store).refresh(scene) if scene else {}
            if any(result.values()):
                logger.log(23, f"Living World guard upkeep: {result}")
        except Exception:
            self.errors += 1
            logger.exception("Living World: guard upkeep failed; carrying on")

    def _guard_lines(self, npc_id: int, name: str) -> list[str]:
        if not self.guard:
            return []
        try:
            scene = self._guard_scene()
            return self._guard_for(self.store).lines_for(npc_id, name, scene) if scene else []
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not add guard lines; leaving them out")
            return []

    def _guard_actions(self, sentence: Any) -> dict[str, Any]:
        """Handle EnlistRecruit / ReportDuty / PayWages on this line (once) and return the reply keys it needs."""
        if not self.guard or self._context is None or id(sentence) in self._guard_sent:
            return {}
        wanted = [a for a in self._line_actions(sentence) if a.get("identifier") in GUARD_ACTIONS]
        if not wanted:
            return {}
        self._guard_sent.add(id(sentence))
        self._release_held(sentence, GUARD_ACTIONS)
        scene = self._guard_scene()
        speaker = sentence.speaker
        if scene is None or speaker is None or speaker.is_player_character:
            return {}
        keys: dict[str, Any] = {}
        guard = self._guard_for(self.store)
        for action in wanted:
            # the leader: the NPC named in the call (advanced actions), else the speaker, else the one leader present
            source = (action.get("arguments") or {}).get("source")
            name = source[0] if isinstance(source, list) and source else source
            who = next((c for c in self._context.npcs_in_conversation.get_all_characters()
                        if name and not c.is_player_character and c.name.lower() == str(name).lower()), speaker)
            npc_id = self._npc_ids.get(who.ref_id) or self._register(who, scene.now)
            npc_id = (guard.leader_for(npc_id, scene) if action["identifier"] == GUARD_ENLIST
                      else guard.superior_for(npc_id, scene))
            result, issue = guard.action(action["identifier"], npc_id, scene)
            logger.log(23, f"Living World guard: {who.name} {action['identifier']}: {result}")
            if issue is not None and issue.key_value():
                keys[KEY_GUARD_ISSUE] = issue.key_value()
                keys[KEY_GUARD_ISSUE_IDS] = issue.form_ids()
                keys[KEY_GUARD_ISSUE_PLUGINS] = issue.plugins()
                keys[KEY_GUARD_ISSUE_COUNTS] = issue.item_counts()
        return keys

    # ---- actions that came without a line (tool calls) ----
    # A tool call (from Mantella's separate tool-calling model, or the main model's tool call) reaches the
    # game as an action-only reply before the NPC speaks. The guard's armour, wages and fines ride on a
    # spoken line (the game hands them over with it), so those actions are held for the speaker's next line.
    HELD_ACTIONS = (*GUARD_ACTIONS, decency.FINE_ACTION)

    @_never_raises
    def action_only(self, sentence: Any) -> None:
        """An action-only reply went to the game: keep the actions that need a spoken line for the next one."""
        speaker = getattr(sentence, "speaker", None)
        if self._context is None or speaker is None or speaker.is_player_character:
            return
        held = [a for a in (getattr(sentence, "actions", None) or [])
                if isinstance(a, dict) and a.get("identifier") in self.HELD_ACTIONS]
        if held:
            self._held_actions.setdefault(speaker.ref_id, []).extend(held)
            logger.log(23, f"Living World: holding {[a['identifier'] for a in held]} for {speaker.name}'s next line")

    def _line_actions(self, sentence: Any) -> list[dict[str, Any]]:
        """The actions on this line, plus any held for its speaker from an action-only reply."""
        own = [a for a in (getattr(sentence, "actions", None) or []) if isinstance(a, dict)]
        speaker = getattr(sentence, "speaker", None)
        held = self._held_actions.get(speaker.ref_id, []) if speaker is not None else []
        seen = {a.get("identifier") for a in own}
        return own + [a for a in held if a.get("identifier") not in seen]

    def _release_held(self, sentence: Any, identifiers: Any) -> None:
        speaker = getattr(sentence, "speaker", None)
        if speaker is None or speaker.ref_id not in self._held_actions:
            return
        rest = [a for a in self._held_actions[speaker.ref_id] if a.get("identifier") not in identifiers]
        if rest:
            self._held_actions[speaker.ref_id] = rest
        else:
            del self._held_actions[speaker.ref_id]

    @_never_raises
    def filter_tool_actions(self, actions: list, messages: Any, characters: Any) -> list:
        """Tool calls before they go to the game: AddToConversation only for people the player asked for."""
        if not self.asked_joins or not actions or not characters.contains_player_character():
            return actions
        from livingworld.dialogue.group import last_user_text
        said = last_user_text(messages)
        kept, dropped = joins.keep_asked(actions, said)
        if dropped:
            logger.log(23, f"Living World: not adding {', '.join(dropped)} to the conversation (the player did not ask)")
        return kept

    def _thane_hold(self, db, guard_id: int, player_id: int) -> str | None:
        """The hold ``guard_id`` serves, when the player is its thane (the game's own word, or a debug grant)."""
        from livingworld.guard import orgs as GO, profiles as GP, store as GS
        p = GP.get(db, guard_id)
        org = GO.by_key(p.org_key) if p is not None else None
        if org is None:
            return None
        if org.hold in self._thane_of or GS.last_event_gt(db, "thane", player_id, org.key) is not None:
            return org.hold
        return None

    def _decency_fine(self, sentence: Any) -> dict[str, Any]:
        """PayFine on a guard's line (once): the gold the game takes from the player for public nakedness."""
        if not self.decency or self._context is None or self.store is None or id(sentence) in self._fine_sent:
            return {}
        if not any(a.get("identifier") == decency.FINE_ACTION for a in self._line_actions(sentence)):
            return {}
        self._fine_sent.add(id(sentence))
        self._release_held(sentence, (decency.FINE_ACTION,))
        speaker = sentence.speaker
        player = self._context.npcs_in_conversation.get_player_character()
        if speaker is None or speaker.is_player_character or player is None:
            return {}
        now = self._context.game_days
        guard_id = self._npc_ids.get(speaker.ref_id) or self._register(speaker, now)
        player_id = self._npc_ids.get(player.ref_id) or self._register(player, now)
        with self.store.transaction() as db:
            gold = decency.pay(db, guard_id, player_id, now, getattr(self._context, "location", None),
                               thane=self._thane_hold(db, guard_id, player_id) is not None)
        logger.log(23, f"Living World decency: {speaker.name} PayFine -> {gold} gold")
        return {decency.KEY_FINE_GOLD: gold, decency.KEY_FINE_GUARD: speaker.name} if gold else {}

    def _carried_mood(self, npc_id: int, now: float) -> "moods.Mood | None":
        try:
            with self.store.transaction() as db:
                return moods.carried_over(db, npc_id, now)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not read an NPC's last mood; leaving it out")
            return None

    # ---- mood meter (called from ChatManager and GameStateManager) ----
    @_never_raises
    def mood_filter(self, character: Any, characters: Any) -> "moods.MoodFilter | None":
        """A filter that cuts mood tags out of ``character``'s streamed reply, or None when the meter is off."""
        if not self.mood_meter or character.is_player_character or not self.handles_turn(characters):
            return None
        return moods.MoodFilter(lambda mood: self._mood_seen(character, mood))

    @_never_raises
    def mood_messages(self, messages: Any, character: Any) -> Any:
        """The messages for this NPC's AI call, with a reminder to open with a mood tag (never stored)."""
        try:
            return moods.with_reminder(messages, character.name)
        except Exception:
            logger.exception("Living World: could not add the mood reminder; sending the messages as they are")
            return messages

    def _mood_seen(self, character: Any, mood: "moods.Mood") -> None:
        now = self._context.game_days
        npc_id = self._npc_ids.get(character.ref_id) or self._register(character, now)
        if self.body_language:   # the model's mood tag is advice: rules cap how far and which way it moves
            previous = self._carried_mood(npc_id, now)
            score = self._body_for(self.store).clamp_mood(npc_id, mood.score, previous.score if previous else None, now)
            if score != mood.score:
                logger.log(23, f"Living World mood: {character.name} said {mood.label} {mood.score}, kept at {score}")
                label = mood.label if abs(score - mood.score) <= 1 else moods.describe(score)
                mood = moods.Mood(label, score)
        with self.store.transaction() as db:
            moods.save(db, npc_id, mood, now)
            stage = drink_state(db, npc_id, now).stage
        if self.tasks:
            scene = self._player_scene()
            if scene is not None:
                self._work_for(self.store).mood(npc_id, mood.score, scene)
        text = moods.meter_text(character.name, mood, stage)
        logger.log(23, f"Living World mood: {text}")
        if self._moods_shown.get(npc_id) != text:
            self._moods_shown[npc_id] = text
            self._moods_to_send[character.ref_id] = text

    @_never_raises
    def line_reply_keys(self, sentence: Any) -> dict[str, Any]:
        """Extra keys for an NPC line sent to the game: the speaker's mood meter, when it changed."""
        speaker = getattr(sentence, "speaker", None)
        if speaker is None or sentence.is_narration:
            return {}
        keys = {}
        try:
            keys.update(self._decency_fine(sentence))
        except Exception:
            self.errors += 1
            logger.exception("Living World: a decency fine failed; carrying on without it")
        try:
            keys.update(self._guard_actions(sentence))
        except Exception:
            self.errors += 1
            logger.exception("Living World: a guard action failed; carrying on without it")
        text = self._moods_to_send.pop(speaker.ref_id, None)
        if text:
            keys[KEY_MOOD] = text
        if self._fog_active() and self.store is not None and self.conv_id is not None:
            try:
                keys.update(self._fog_line_keys(sentence))
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not check an NPC line for names and places; carrying on")
        profiles = self._body_profiles()
        if profiles:
            keys[KEY_BODY] = profiles
        if self.guard and self.store is not None:     # each NPC line: joining or leaving can happen mid-conversation
            try:
                scene = self._guard_scene()
                change = self._guard_for(self.store).bed_change(scene) if scene else None
                if change is not None:
                    bed, own = change
                    keys[KEY_GUARD_BED_ID] = int(bed.form_id, 16)
                    keys[KEY_GUARD_BED_PLUGIN] = bed.plugin
                    keys[KEY_GUARD_BED_OWN] = own
                    logger.log(23, f"Living World guard: barracks bunk {bed.form_id} {'given' if own else 'taken back'}")
                chest = self._guard_for(self.store).chest_due(scene) if scene else None
                if chest is not None:
                    keys[KEY_GUARD_CHEST_ID] = int(chest.form_id, 16)
                    keys[KEY_GUARD_CHEST_PLUGIN] = chest.plugin
                    logger.log(23, f"Living World guard: footlocker {chest.form_id} to be placed by the bunk")
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not check the barracks bunk; carrying on")
        if self.guard and self.store is not None:
            try:
                keys.update(self._arrest_keys())
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not send a guard arrest to the game; carrying on")
        if self.guard and self.store is not None:
            try:
                keys.update(self._watch_keys())
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not send the guard watch windows; carrying on")
        if self.guard and self.store is not None:
            self._hints_to_send += self._guard_for(self.store).take_notices()   # shift started / over
        if self._trips is not None:
            self._hints_to_send += self._trips.take_notices()   # "Go with Alfhild to Battle-Born Farm? Say yes or no."
        if self._hints_to_send:
            keys[KEY_HINTS] = self._hints_to_send
            self._hints_to_send = []
        return keys

    def _arrest_keys(self) -> dict[str, Any]:
        """G2f: an arrest the rules decided on (one per line) and prisoners to let out."""
        guard = self._guard_for(self.store)
        orders, releases = guard.take_arrest_orders()
        keys: dict[str, Any] = {}
        if orders:
            o = orders[0]
            guard.arrest_orders = orders[1:] + guard.arrest_orders   # any others go with the next line
            keys.update({KEY_GUARD_ARREST_NAME: o.name, KEY_GUARD_ARREST_MODE: o.mode, KEY_GUARD_ARREST_ID: o.arrest_id,
                         KEY_GUARD_ARREST_DAYS: o.days})
            if o.jail is not None and o.jail.form_id:
                keys[KEY_GUARD_JAIL_ID] = int(o.jail.form_id, 16)
                keys[KEY_GUARD_JAIL_PLUGIN] = o.jail.plugin
            logger.log(23, f"Living World guard: arrest {o.arrest_id} sent to the game: {o.name} {o.mode}")
        if releases:
            keys[KEY_GUARD_RELEASE] = releases
            logger.log(23, f"Living World guard: prisoners to let out: arrests {releases}")
        return keys

    def _watch_keys(self) -> dict[str, Any]:
        """G2e: the duty's watch windows, sent with the first NPC line of a conversation and whenever they change."""
        scene = self._guard_scene()
        plan = self._guard_for(self.store).watch_plan(scene) if scene else None
        if plan is None or plan.signature() == self._watch_sent:
            return {}
        self._watch_sent = plan.signature()
        from livingworld.guard import encounter as ENC, orgs as O
        cooldown = 3.0
        if plan.task_id:
            with self.store.transaction() as db:
                from livingworld.guard import store as GS
                svc = GS.current(db, scene.player_id)
                org = O.by_key(svc.org_key) if svc else None
                cooldown = ENC.config(org.key).npc_cooldown_days if org else cooldown
            logger.log(23, f"Living World guard: watch windows for duty {plan.task_id}: " + ", ".join(
                f"{w.win}@{w.at:.4f} {w.kind}" for w in plan.windows))
        return {KEY_GUARD_WATCH_TASK: plan.task_id, KEY_GUARD_WATCH_END: plan.end,
                KEY_GUARD_WATCH_AT: [w.at for w in plan.windows], KEY_GUARD_WATCH_WIN: [w.win for w in plan.windows],
                KEY_GUARD_WATCH_KIND: [1 if w.kind == ENC.APPROACH else 0 for w in plan.windows],
                KEY_GUARD_WATCH_OBS: [w.observation for w in plan.windows], KEY_GUARD_WATCH_COOLDOWN: cooldown}

    def start_reply_keys(self) -> dict[str, Any]:
        """Extra keys for the conversation start reply."""
        keys: dict[str, Any] = {KEY_QUIET_CHATTER: True} if self.quiet_chatter else {}
        keys[KEY_BODY_ON] = bool(self.body_language)
        keys[KEY_GESTURES_ON] = bool(self.body_language and gestures_enabled())
        keys[KEY_GESTURE_NOTICES] = gesture_notices()
        keys[intimate_scenes.KEY_INTIMACY_ON] = bool(self.intimacy)
        keys[confront.KEY_CONFRONT_ON] = bool(self.confront)
        keys[decency.KEY_DECENCY_ON] = bool(self.decency)
        keys[surrender.KEY_SURRENDER_ON] = bool(self.surrender)
        keys[KEY_PEEK_ON] = bool(self.peek and self.body_language)
        if self.confront and self.store is not None:
            try:
                with self.store.reading() as db:
                    keys[confront.KEY_COUPLES] = confront.couples(db)
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not list the couples for the game; carrying on")
        if self.intimacy:
            from livingworld.intimacy import arousal
            keys[arousal.KEY_AROUSAL_KEY] = arousal.arousal_key()
        if self.body_language and self.store is not None:
            self._body_for(self.store).forget_sent()   # the first NPC line sends every participant's profile again
        wanting = self._wants_a_word()
        if wanting:
            keys[KEY_WANTS_A_WORD] = wanting
        keys[fog.KEY_FOG_ON] = bool(self.fog)
        if self.fog and self.store is not None:
            try:
                with self.store.reading() as db:
                    player = fog_names.player_id(db)
                    keys[fog.KEY_FOG_KNOWN] = fog_names.known_to_player(db, player) if player is not None else []
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not list the people the player knows; carrying on")
        self._fog_reveal = []
        self._fog_notes = []
        keys.update(self._diary_keys())
        return keys

    # ---- diaries (livingworld/diary) ----
    def _diary_keepers(self) -> list[tuple[Any, int]]:
        """(character, npc_id) for each participant who keeps a diary now."""
        if not self.diary or self.store is None or self._context is None:
            return []
        out = []
        with self.store.reading() as db:
            player = diaries.player_id(db)
            for c in self._context.npcs_in_conversation.get_all_characters():
                if c.is_player_character:
                    continue
                npc_id = self._npc_ids.get(c.ref_id)
                if npc_id is not None and diaries.is_keeper(db, npc_id, player):
                    out.append((c, npc_id))
        return out

    def _diary_keys(self) -> dict[str, Any]:
        keys: dict[str, Any] = {diary.KEY_DIARY_ON: bool(self.diary)}
        if not self.diary:
            return keys
        try:
            refs, ids, titles = [], [], []
            keepers = self._diary_keepers()
            with self.store.reading() as db:
                player = diaries.player_id(db)
                for character, npc_id in keepers:
                    raw = self._ref_raw.get(character.ref_id)
                    if raw is None:
                        continue
                    refs.append(raw)
                    ids.append(npc_id)
                    titles.append(diaries.title(db, npc_id, player, self._fog_active()))
            if refs:
                keys[diary.KEY_DIARY_REF_IDS] = refs
                keys[diary.KEY_DIARY_NPC_IDS] = ids
                keys[diary.KEY_DIARY_TITLES] = titles
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not list the diary keepers; carrying on")
        return keys

    def _queue_diaries(self, game_days: float) -> None:
        """After a conversation: each keeper in it writes (or rewrites) the day's entry, after the extraction
        job has stored this conversation's memories (jobs run in the order queued)."""
        with self.store.reading() as db:
            player = diaries.player_id(db)
            people = [r[0] for r in db.execute("SELECT npc_id FROM participant WHERE conv_id = ?", (self.conv_id,))]
            keepers = [n for n in people if diaries.is_keeper(db, n, player)]
        queue = JobQueue(self.store)
        for npc_id in keepers:
            queue.enqueue(diary.JOB, {"npc_id": npc_id, "player_id": player, "conv_id": self.conv_id,
                                      "day": int(game_days), "game_days": game_days,
                                      "bio": self._bios.get(npc_id)},
                          f"diary:conv:{self.conv_id}:{npc_id}")

    def diary_read(self, request: dict[str, Any]) -> dict[str, Any]:
        """The game opened a diary: the pages to show, and who (if anyone) caught the player reading it.
        Never answers with an error reply (the game would end a running conversation on one)."""
        reply: dict[str, Any] = {comm_consts.KEY_REPLYTYPE: diary.REPLY_READ, diary.KEY_PAGES: [],
                                 diary.KEY_TITLE: "Diary"}
        if not self.diary:
            reply[diary.KEY_PAGES] = ["The pages are blank."]
            return reply
        try:
            world_id = self.world_for_name(str(request.get(diary.KEY_WORLD_ID) or ""))
            store = self._store_for(world_id) if world_id else self.store
            if store is None:
                reply[diary.KEY_PAGES] = ["The handwriting is too cramped to make out."]
                return reply
            owner = int(request.get(diary.KEY_OWNER_NPC_ID) or 0)
            with store.reading() as db:
                player = diaries.player_id(db)
                known = db.execute("SELECT 1 FROM npc WHERE npc_id = ? AND is_player = 0", (owner,)).fetchone()
                if not known or player is None:
                    reply[diary.KEY_PAGES] = ["The handwriting is too cramped to make out."]
                    return reply
                reply[diary.KEY_TITLE] = diaries.title(db, owner, player, self._fog_active())
                reply[diary.KEY_PAGES] = diaries.pages(db, owner) or [
                    "Only a few lines so far, crossed out and written over. Nothing you can make out."]
            game_time = request.get(diary.KEY_GAME_DAYS)
            if game_time is None:
                game_time = store.scalar("SELECT MAX(game_time) FROM utterance") or 0.0
            diaries.record_read(store, owner, player, bool(request.get(diary.KEY_SEEN_BY_OWNER)),
                                list(request.get(diary.KEY_WITNESS_REF_IDS) or []),
                                list(request.get(diary.KEY_WITNESS_NAMES) or []),
                                request.get(diary.KEY_LOCATION) or None, float(game_time))
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not open the diary; carrying on")
            if not reply[diary.KEY_PAGES]:
                reply[diary.KEY_PAGES] = ["The handwriting is too cramped to make out."]
        return reply

    # ---- fog: names and places unknown until told (livingworld/fog) ----
    def _fog_active(self) -> bool:
        """Fog is on and the game build takes part (an older game build shows every name, so the NPCs are not
        told to pretend otherwise)."""
        return bool(self.fog and self._fog_game)

    @_never_raises
    def fog_summary(self, characters: list[Any], text: str) -> str:
        """Mantella's own summaries name the player; when none of the NPCs here was told that name, they read
        'the stranger' instead."""
        if not self._fog_active() or self.store is None or self._context is None or not text:
            return text
        player = self._context.npcs_in_conversation.get_player_character()
        if player is None:
            return text
        player_id = self._npc_ids.get(player.ref_id) or self._register(player, self._context.game_days)
        player_p = fog_names.Person(player_id, player.name)
        npcs = [self._npc_ids.get(c.ref_id) or self._register(c, self._context.game_days)
                for c in characters if not c.is_player_character]
        with self.store.reading() as db:
            if not npcs or any(fog_names.known_name(db, n, player_id) is not None for n in npcs):
                return text
            return fog_names.hide_player_name(db, npcs[0], player_p, text)

    def _fog_person(self, character: Any) -> "fog_names.Person":
        """Registers the character if need be, so call it outside a transaction."""
        npc_id = self._npc_ids.get(character.ref_id) or self._register(character, self._context.game_days)
        label = self.store.scalar("SELECT stranger_label FROM npc WHERE npc_id = ?", (npc_id,))
        return fog_names.Person(npc_id, character.name, label)

    def _fog_people(self, context: Any) -> tuple["fog_names.Person", list["fog_names.Person"]]:
        player = context.npcs_in_conversation.get_player_character()
        people = [self._fog_person(c) for c in context.npcs_in_conversation.get_all_characters()]
        player_p = next(p for p in people if player is not None and p.npc_id == self._npc_ids.get(player.ref_id))
        return player_p, people

    def _fog_note(self, learned: "fog_names.Learned") -> None:
        for line in learned.log:
            logger.log(23, f"Living World fog: {line}")
        self._fog_reveal += [n for n in learned.player_learned if n not in self._fog_reveal]
        self._fog_notes += [n for n in learned.notes if n not in self._fog_notes]

    @_never_raises
    def fog_notes(self, text: str, context: Any) -> list[str]:
        """Notes for the dialogue model with the player's line: names learned since the last one, the player's
        own introduction included. The system prompt is written once per conversation, so this is how a name
        learned mid-conversation reaches it."""
        if not self._fog_active() or self.conv_id is None or context is not self._context:
            return []
        if text and text.strip():
            player_p, present = self._fog_people(context)
            with self.store.transaction() as db:
                self._fog_note(fog_names.player_line(db, player_p, text.strip(), present, context.game_days,
                                                     self.conv_id))
        notes, self._fog_notes = self._fog_notes, []
        return notes

    def _fog_line_keys(self, sentence: Any) -> dict[str, Any]:
        """Names and places said in this NPC line: the player learns them as the line is spoken."""
        context = self._context
        text = (sentence.text or "").strip()
        keys: dict[str, Any] = {}
        if text and context is not None and context.npcs_in_conversation.get_player_character() is not None:
            player_p, present = self._fog_people(context)
            with self.store.transaction() as db:
                speaker = next((p for p in present if p.npc_id == self._npc_ids.get(sentence.speaker.ref_id)), None)
                if speaker is not None:
                    self._fog_note(fog_names.npc_line(db, speaker, text, present, player_p, context.game_days,
                                                      self.conv_id))
                    markers = fog_places.npc_line(db, text, speaker.npc_id, context.game_days, self.conv_id)
                    if markers:
                        keys[fog.KEY_FOG_MARKER_IDS] = [m.form_int for m in markers]
                        keys[fog.KEY_FOG_MARKER_PLUGINS] = [m.plugin for m in markers]
                        keys[fog.KEY_FOG_MARKER_NAMES] = [m.name for m in markers]
                        logger.log(23, f"Living World fog: {speaker.name} told the player about "
                                       + ", ".join(m.name for m in markers))
        if self._fog_reveal:
            keys[fog.KEY_FOG_REVEAL] = self._fog_reveal
            self._fog_reveal = []
        return keys

    def _wants_a_word(self) -> str:
        """'Gerdur|Skyrim.esm|0x0001347C;...': people with work they would offer the player now."""
        if not (self.tasks or self.guard) or self.store is None or self._context is None:
            return ""
        try:
            scene = self._player_scene()
            ids = self._work_for(self.store).wants_a_word(scene) if scene and self.tasks else []
            if self.guard:
                guard_scene = self._guard_scene()
                ids += [i for i in (self._guard_for(self.store).wants_a_word(guard_scene) if guard_scene else [])
                        if i not in ids]
            rows = [self.store.query("SELECT display_name, plugin, base_form_id FROM npc WHERE npc_id = ?", (i,))
                    for i in ids]
            return ";".join(f"{r[0][0]}|{r[0][1]}|{r[0][2]}" for r in rows if r)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not list who wants a word; leaving it out")
            return ""

    # ---- body language (livingworld/bodylang) ----
    def _body_for(self, store: WorldStore) -> BodyLanguage:
        key = id(store)
        if key not in self._body:
            self._body[key] = BodyLanguage(store, peek=bool(self.peek))
        return self._body[key]

    def _body_lines(self, npc_id: int, name: str, now: float) -> list[str]:
        if not self.body_language or self.store is None:
            return []
        try:
            return self._body_for(self.store).prompt_lines(npc_id, name, now)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not tell what an NPC noticed of body language; leaving it out")
            return []

    def body_events(self, context: Any) -> list[str]:
        """Body language noticed since the prompt was built, as in-game events on the player's next line.
        Each observation is told once: here, or in the prompt when a conversation starts."""
        if not self.body_language or self.store is None or context is None:
            return []
        try:
            now = context.game_days
            out = []
            for c in context.npcs_in_conversation.get_all_characters():
                if c.is_player_character:
                    continue
                npc_id = self._npc_ids.get(c.ref_id) or self._register(c, now)
                lines = self._body_for(self.store).prompt_lines(npc_id, c.name, now)
                out += [f"{c.name} noticed: {line}"
                        if not (line.startswith("Because of this") or line.startswith(c.name)) else line
                        for line in lines]
            if out:
                logger.log(23, "Living World body language: " + " / ".join(out))
            return out
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not add body language events; leaving them out")
            return []

    def scene_notes(self, context: Any) -> list[str]:
        """A note on the player's line while someone in the conversation is in, or watching, an intimate scene.
        The prompt already says so, but far from the reply; without this the NPCs carried on with the earlier
        topic as if nothing were happening."""
        if not self.intimacy or self.store is None or context is None or not self._scenes_now:
            return []
        try:
            now = context.game_days
            out = []
            for c in context.npcs_in_conversation.get_all_characters():
                if c.is_player_character:
                    continue
                npc_id = self._npc_ids.get(c.ref_id) or self._register(c, now)
                line = intimate_scenes.turn_note(npc_id, c.name, self._scenes_now)
                if line:
                    out.append(line)
            return out
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not add the intimate scene note; leaving it out")
            return []

    def _body_profiles(self) -> list[str]:
        if not self.body_language or self.store is None or self._context is None:
            return []
        try:
            now = self._context.game_days
            player = self._context.npcs_in_conversation.get_player_character()
            player_id = (self._npc_ids.get(player.ref_id) or self._register(player, now)) if player else None
            people = {}
            for c in self._context.npcs_in_conversation.get_all_characters():
                if not c.is_player_character:
                    people[self._npc_ids.get(c.ref_id) or self._register(c, now)] = c.name
            return self._body_for(self.store).profiles(people, player_id, now)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not work out body language profiles; the game keeps its last ones")
            return []

    def _guards_here(self, db, present: dict[int, str]) -> set[int]:
        """The people in this conversation who are guards: the game's word (IsGuard) or a guard career profile."""
        out: set[int] = set()
        if self._context is not None:
            for c in self._context.npcs_in_conversation.get_all_characters():
                f = self._guard_facts.get(c.ref_id)
                if f is not None and f.is_guard and c.ref_id in self._npc_ids:
                    out.add(self._npc_ids[c.ref_id])
        for npc_id in present:
            if db.execute("SELECT 1 FROM guard_profile WHERE npc_id = ?", (npc_id,)).fetchone():
                out.add(npc_id)
        return out

    def _noticed(self, npc_id: int, name: str, now: float, present: dict[int, str], player_id: int | None = None,
                 player_name: str | None = None) -> list[str]:
        try:
            with self.store.transaction() as db:
                scene_lines = []
                if self.decency and player_id is not None and npc_id != player_id:
                    told = decency.line(db, npc_id, name, player_id, player_name or "the player", now,
                                        thane_of=self._thane_hold(db, npc_id, player_id))
                    if told:
                        scene_lines.append(told)
                if self.surrender and player_id is not None and npc_id != player_id:
                    begged = surrender.line(db, npc_id, name, player_id, player_name or "the player", now)
                    if begged:
                        scene_lines.append(begged)
                    if self.wingman:
                        wing = wingman.line(db, npc_id, name, player_id, player_name or "the player", present,
                                            self._guards_here(db, present), now)
                        if wing:
                            scene_lines.append(wing)
                if self.intimacy and self._scenes_now:
                    from livingworld.perception.attire import sex_of
                    from livingworld.intimacy.common import partner_in, partners_of
                    watched_by = set()
                    if self.confront:
                        for s in self._scenes_now:
                            if npc_id in s.people:
                                watched_by |= {w for w in s.seen_by if partner_in(db, w, [npc_id]) == npc_id}
                    scene_lines += intimate_scenes.lines_for(npc_id, name, self._scenes_now, present,
                                                             is_woman=sex_of(db, npc_id) == "female",
                                                             partners=partners_of(db, npc_id, self._scenes_now),
                                                             confront=self.confront, watched_by=watched_by)
                if self.confront and not is_child(db, npc_id):
                    scene_lines += confront.grievance_lines(db, npc_id, name, now, present)
                if self.intimacy and not is_child(db, npc_id):
                    from livingworld.intimacy import arousal, climax
                    with_player = any(npc_id in s.people and s.player_id is not None for s in self._scenes_now)
                    scene_lines += arousal.lines_for(db, npc_id, name, present, self._attire_now, with_player)
                    scene_lines += climax.lines_for(db, npc_id, name, now, present, self._attire_now)
                return scene_lines + awareness.lines_for(db, npc_id, name, now, present, self._attire_now,
                                                         hygiene=self.hygiene)
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not work out what an NPC notices; leaving it out")
            return []

    # ---- dialogue (called from ChatManager) ----
    @property
    def context(self) -> Any:
        return self._context

    def handles_turn(self, characters: Any) -> bool:
        """Living World generates the turn when a conversation with the player is open."""
        return (self.enabled and self.conv_id is not None and self._context is not None
                and characters.contains_player_character())

    def run_turn(self, chat_manager: Any, messages: Any, characters: Any, queue: Any, actions: list,
                 tools: list | None, game: Any) -> None:
        if characters.contains_multiple_npcs():
            from livingworld.dialogue.group import GroupTurn
            GroupTurn(self).run(chat_manager, messages, characters, queue, actions, tools, game)
            return
        import asyncio
        import time
        speaker = characters.last_added_character
        started = time.perf_counter()
        status, error = "ok", None
        try:
            asyncio.run(chat_manager.process_response(speaker, queue, messages, characters, actions, tools, game))
        except Exception as e:
            status, error = "error", str(e)
            raise
        finally:
            self.record_dialogue_call(chat_manager, speaker, int((time.perf_counter() - started) * 1000), status, error)

    @_never_raises
    def record_dialogue_call(self, chat_manager: Any, speaker: Any, latency_ms: int, status: str,
                             error: str | None) -> None:
        """One ``llm_request`` row per dialogue call, with the speaker and the rows their prompt held."""
        import json
        npc_id = self._npc_ids.get(speaker.ref_id) or self._register(speaker, self._context.game_days)
        try:
            model = chat_manager._get_per_character_client(speaker).model_name
        except Exception:
            model = "unknown"
        know_ids, mem_ids = self._last_recall.get(npc_id, ([], []))
        with self.store.transaction() as db:
            timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
            trace = db.execute(
                "INSERT INTO decision_trace (timeline_id, npc_id, summary, inputs_json, game_time) VALUES (?,?,?,?,?)",
                (timeline, npc_id, f"dialogue prompt for {speaker.name}",
                 json.dumps({"know": know_ids, "mems": mem_ids}), self._context.game_days)).lastrowid
            db.execute(
                "INSERT INTO llm_request (timeline_id, task, model, npc_id, conv_id, trace_id, latency_ms, status, error)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (timeline, "dialogue", str(model or "unknown"), npc_id, self.conv_id, trace, latency_ms, status, error))

    def promise_parser(self, actions: list) -> Any:
        """A reply parser that starts Sit / Drink when the NPC says they are about to (dialogue/promises.py),
        and asks the player before an NPC who agreed to go somewhere sets off (dialogue/trips.py)."""
        also = []
        if self._trips is not None:
            from src.actions.function_manager import FunctionManager
            also = [trips.TripWatcher(self._trips, actions, FunctionManager._actions.keys())]
        if not self.keep_promises and not also:
            return None
        return promises.make_parser(actions, keep_promises=self.keep_promises, also=also)

    @_never_raises
    def trip_notes(self, text: str, context: Any) -> list[str]:
        """The player's answer to "Go with Alfhild to Battle-Born Farm?", as a note for the dialogue model."""
        if self._trips is None or context is not self._context or not text or not text.strip():
            return []
        player = context.npcs_in_conversation.get_player_character()
        if player is not None:
            self._trips.player_name = player.name
        here = str(getattr(context, "location", "") or "")
        if here:
            self._trips.here = here
        for c in context.npcs_in_conversation.get_all_characters():
            if not c.is_player_character:
                self._trips.learn_bio(c.name, str(getattr(c, "bio", "") or ""))   # "the farm" = her farm
        note = self._trips.player_said(text.strip())
        return [note] if note else []

    def voice_allowed(self, character: Any) -> bool:
        """False if this NPC's bound voice differs from the voice Mantella is about to use."""
        if not self.enabled or self.store is None or character.is_player_character:
            return True
        try:
            npc_id = self._npc_ids.get(character.ref_id)
            if npc_id is None:
                return True
            bound = self.store.scalar("SELECT voice_model FROM voice_binding WHERE npc_id = ?", (npc_id,))
        except Exception:
            logger.exception("Living World: voice check failed; allowing the line")
            return True
        if bound is not None and bound != character.tts_voice_model:
            self.errors += 1
            logger.error(f"Living World: refused to voice {character.name} with '{character.tts_voice_model}'; "
                         f"their voice is bound to '{bound}'. No audio was made for this line. To change the voice on "
                         f"purpose, delete the voice_binding row for npc_id {npc_id}.")
            return False
        return True

    def wait_for_jobs(self) -> int:
        """Run every due job now, on this thread. Returns how many ran."""
        return sum(worker.run_pending() for worker in self._workers.values())

    def stop(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        if self.perf_monitor:
            self.perf_monitor.stop()

    # ---- internals ----
    def _store_for(self, world_id: str) -> WorldStore:
        if world_id not in self._stores:
            store = WorldStore.for_world(self.save_folder, world_id)
            self._stores[world_id] = store
            worker = JobWorker(store, lambda kind, payload, store=store: self._run_job(store, kind, payload))
            self._workers[world_id] = worker
            try:   # conversations whose extraction learned nothing (the old reply cap) get one more go
                queued = backfill.queue_backfill(store)
                if queued:
                    logger.log(23, f"Living World: queued {queued} past conversations to extract memories from again")
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not queue the memory backfill; carrying on")
            if self.start_workers:
                worker.start()
        return self._stores[world_id]

    def _run_job(self, store: WorldStore, kind: str, payload: dict[str, Any]) -> None:
        if kind == diary.JOB:
            if self.complete is None:
                raise RuntimeError("no LLM attached for the diary")
            diaries.write_entry(store, self.complete, payload)
            return
        if kind != EXTRACT_JOB:
            raise ValueError(f"unknown job kind {kind}")
        if self.complete is None:
            raise RuntimeError("no LLM attached for extraction")
        Extractor(store, self.complete).extract_conversation(payload["conv_id"], bool(payload.get("backfill")))

    def _close(self, game_days: float | None) -> None:
        if game_days is None:
            game_days = self.store.scalar(
                "SELECT COALESCE(MAX(game_time), (SELECT started_gt FROM conversation WHERE conv_id = ?))"
                " FROM utterance WHERE conv_id = ?", (self.conv_id, self.conv_id))
        self._store_running_scenes()
        ConversationLog(self.store).close(self.conv_id, game_days)
        if self.tasks:
            try:
                declined = self._work_for(self.store).conversation_closed(game_days)
                if declined:
                    logger.log(23, f"Living World work: offers not taken up: tasks {declined}")
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not close task offers")
        logger.log(23, f"Living World: conversation {self.conv_id} closed at game day {game_days:.2f}")
        if self.store.scalar("SELECT COUNT(*) FROM utterance WHERE conv_id = ?", (self.conv_id,)):
            JobQueue(self.store).enqueue(EXTRACT_JOB, {"conv_id": self.conv_id}, f"extract:conv:{self.conv_id}")
            if self.diary:
                try:
                    self._queue_diaries(game_days)
                except Exception:
                    self.errors += 1
                    logger.exception("Living World: could not queue the diary entries; carrying on")
            for world_id, store in self._stores.items():
                if store is self.store:
                    self._workers[world_id].notify()
        self.conv_id = None
        self._context = None

    def _store_running_scenes(self) -> None:
        """Scenes the last snapshot showed still running: stored now, marked ongoing, because the game only sends
        a scene's end record with its next request, and that may not come (2026-11-11: the second scene with
        Gisli ran 17:00-17:09 after the last conversation and was never stored). The end record, if it does come,
        completes the stored event (``intimacy/scenes.record_scene``)."""
        records, self._scene_records = self._scene_records, []
        if not self.intimacy or self.store is None or not records:
            return
        try:
            pipeline = PerceptionPipeline(self.store)
            stored = 0
            for record in self._scene_seers.open_records(records):
                outcome = intimate_scenes.record_scene(pipeline, record, ongoing=True)
                stored += outcome.event_id is not None and not outcome.duplicate
            if stored:
                self._social_for(self.store).appraise_new()
        except Exception:
            self.errors += 1
            logger.exception("Living World: could not store a scene still running at the end of the conversation")

    def _sync(self, context: Any) -> None:
        log = ConversationLog(self.store)
        now_present: set[int] = set()
        for character in context.npcs_in_conversation.get_all_characters():
            npc_id = self._npc_ids.get(character.ref_id) or self._register(character, context.game_days)
            now_present.add(npc_id)
            log.add_participant(self.conv_id, npc_id)
            if character.ref_id in self._fog_labels:
                with self.store.transaction() as db:
                    fog_names.set_label(db, npc_id, self._fog_labels[character.ref_id])
        for npc_id in self._present - now_present:
            log.mark_left(self.conv_id, npc_id)
        self._present = now_present

    def _register(self, character: Any, game_time: float) -> int:
        identity = self._identities.get(character.ref_id) or self._identity_from_character(character)
        registry = IdentityRegistry(self.store)
        npc_id = registry.ensure_npc(identity, game_time, race=character.race, sex=character.gender,
                                     is_generic=character.is_generic_npc,
                                     voice=getattr(character, "in_game_voice_model", None))
        if not character.is_player_character:
            try:
                registry.bind_voice(npc_id, character.tts_service or self.default_tts_service, character.tts_voice_model,
                                    character.in_game_voice_model, game_time)
            except VoiceBindingConflict as e:
                self.errors += 1
                logger.error(f"Living World: {character.name}: {e}")
        self._npc_ids[character.ref_id] = npc_id
        if character.ref_id in self._fog_labels:
            with self.store.transaction() as db:
                fog_names.set_label(db, npc_id, self._fog_labels[character.ref_id])
        if not character.is_player_character and getattr(character, "bio", None):
            self._bios[npc_id] = str(character.bio)
            try:
                with self.store.transaction() as db:
                    seed_traits_from_bio(db, npc_id, character.bio, game_time)
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not read traits from a bio; carrying on")
        if self.guard and not character.is_player_character:
            try:
                self._guard_for(self.store).note(npc_id, self._guard_facts.get(character.ref_id), game_time)
            except Exception:
                self.errors += 1
                logger.exception("Living World: could not note a guard; carrying on")
        return npc_id

    @staticmethod
    def _identity_from_character(character: Any) -> ActorIdentity:
        # Only reached if the request's actor list was not seen first (should not happen in the game).
        base = FormId("unknown", "0x00" + character.base_id, character.base_id)
        ref = FormId("unknown", "0x00" + character.ref_id, character.ref_id)
        return ActorIdentity(character.name, base, ref, character.is_player_character)
