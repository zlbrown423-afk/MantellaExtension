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
from livingworld.memory.recall import recall_for
from livingworld import mood as moods
from livingworld.perception import awareness
from livingworld.perception.intoxication import drink_state
from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.social.world import SocialWorld
from livingworld.work import tasks_enabled
from livingworld.guard import guard_debug_enabled, guard_enabled
from livingworld.guard.profiles import GuardFacts
from livingworld.guard.service import ACTIONS as GUARD_ACTIONS, Guard, Scene as GuardScene
from livingworld.work.service import ACTIONS as TASK_ACTIONS, Scene as WorkScene, Work
from livingworld.perception.records import KEY_ATTIRE, BadRecord, parse_record, records_from_context
from livingworld.perf.game_records import split_perf_records
from livingworld.perf.monitor import PerfMonitor
from livingworld.perf.stats import STATS as PERF
from livingworld.store.conversation_log import ConversationLog
from livingworld.store.timeline import resolve_load
from livingworld.store.identity import ActorIdentity, FormId, IdentityRegistry, VoiceBindingConflict
from livingworld.store.world_store import WorldStore
from src import utils

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
KEY_GUARD_ISSUE = "mantella_lw_guard_issue"      # sent with an NPC line: items handed to the player ('Name|plugin|0xID;...', for logs)
KEY_GUARD_ISSUE_IDS = "mantella_lw_guard_issue_ids"          # ...the same items as plugin-relative form IDs (the game reads these)
KEY_GUARD_ISSUE_PLUGINS = "mantella_lw_guard_issue_plugins"  # ...and the plugin each one comes from
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
        self.tasks = enabled and tasks_enabled()    # NPC tasks and shared work (livingworld/work)
        self._work: dict[int, Work] = {}            # id(store) -> tasks for that world
        self._work_refreshed: int | None = None     # conv_id tasks were last brought up to date for
        self.guard = enabled and guard_enabled()    # guard careers (livingworld/guard)
        self.guard_debug = enabled and guard_debug_enabled()
        self._guard: dict[int, Guard] = {}          # id(store) -> guard careers for that world
        self._guard_facts: dict[str, GuardFacts] = {}   # Mantella's 6-digit ref id -> what the game said
        self._thane_of: tuple[str, ...] = ()        # holds the game says the player is thane of
        self._guard_sent: set[int] = set()          # id(sentence) whose guard actions were handled
        self.perf_monitor: PerfMonitor | None = None  # MANTELLA_LW_PERF=1 (see livingworld/perf)
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
            identity = ActorIdentity.from_actor_json(actor_json)
            self._identities[identity.ref.mantella_id] = identity
            if self.guard and KEY_IS_GUARD in actor_json:
                faction = actor_json.get(KEY_CRIME_FACTION)
                bounty = actor_json.get(KEY_PLAYER_BOUNTY)
                self._guard_facts[identity.ref.mantella_id] = GuardFacts(
                    bool(actor_json.get(KEY_IS_GUARD)), actor_json.get(KEY_CRIME_PLUGIN) or None,
                    f"0x{int(faction) & 0xFFFFFF:08X}" if faction else None,
                    int(bounty) if bounty is not None else None)

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
        if not (raw or attire_raw) or self.store is None:
            return 0
        pipeline = PerceptionPipeline(self.store)
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

    @_never_raises
    def game_loaded(self, world_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Handle the save load the game reports with each conversation start (see ``store/timeline.py``).
        Returns extra reply keys: the timeline id the game should keep in its save."""
        nonce = request.get(KEY_LOAD_NONCE)
        if nonce is None or request.get(KEY_LOADED_GAME_DAYS) is None:
            return {}
        store = self._store_for(world_id)
        save_timeline = request.get(KEY_TIMELINE_ID) or None
        result = resolve_load(store, int(nonce), int(save_timeline) if save_timeline else None,
                              float(request[KEY_LOADED_GAME_DAYS]))
        if not result.repeated:
            if result.branched:
                logger.log(23, f"Living World: an older save was loaded (game day {request[KEY_LOADED_GAME_DAYS]:.2f}); "
                               f"NPCs now forget everything after it (timeline {result.timeline_id}).")
            else:
                logger.log(23, f"Living World: save loaded on timeline {result.timeline_id}.")
        return {KEY_TIMELINE_ID: result.timeline_id}

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
        self._moods_shown = {}
        self._moods_to_send = {}
        self._guard_sent = set()

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

    @_never_raises
    def player_line(self, text: str, context: Any) -> None:
        if self.conv_id is None or not text or not text.strip():
            return
        player = context.npcs_in_conversation.get_player_character()
        if not player:
            return
        npc_id = self._npc_ids.get(player.ref_id) or self._register(player, context.game_days)
        ConversationLog(self.store).append(self.conv_id, npc_id, text.strip(), None, context.game_days)
        if self.hygiene_debug:
            self._hygiene_debug(text, npc_id, context)
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
        self._refresh_social(now, list(present))
        self._refresh_work(now)
        scene = self._scene(player_id, player.name if player else "the player", list(present))
        blocks = []
        for character in characters:
            if character.is_player_character:
                continue
            npc_id = self._npc_ids.get(character.ref_id) or self._register(character, now)
            key = (self.conv_id, npc_id)
            recall = recall_for(store, npc_id, now, player_id, mark_rehearsed=key not in self._rehearsed)
            self._rehearsed.add(key)
            self._last_recall[npc_id] = (recall.know_ids, recall.mem_ids)
            text = recall.text()
            if text:
                blocks.append(text)
            noticed = self._noticed(npc_id, character.name, now, present)
            if noticed:
                blocks.append(f"What {character.name} notices right now:\n" + "\n".join(f"- {line}" for line in noticed))
            if self.mood_meter and player is not None:
                blocks.append("\n".join(moods.prompt_lines(character.name, self._carried_mood(npc_id, now))))
            work = self._work_lines(npc_id, character.name, scene)
            if work:
                blocks.append(f"Work on {character.name}'s mind:\n" + "\n".join(f"- {line}" for line in work))
            guard = self._guard_lines(npc_id, character.name)
            if guard:
                blocks.append(f"The guard, and {character.name}:\n" + "\n".join(f"- {line}" for line in guard))
        return "\n\n".join(blocks)

    def _social_for(self, store: WorldStore) -> SocialWorld:
        if id(store) not in self._social:
            self._social[id(store)] = SocialWorld(store)
        return self._social[id(store)]

    def _refresh_social(self, now: float, present: list[int]) -> None:
        """Once per conversation, before the first prompt: seed, appraise and catch gossip up."""
        if self._social_refreshed == self.conv_id:
            return
        self._social_refreshed = self.conv_id
        try:
            self._social_for(self.store).refresh(now, present)
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
                          getattr(self._context, "location", None), facts, self._thane_of)

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
        """Handle EnlistRecruit on this line (once) and return the reply keys it needs."""
        if not self.guard or self._context is None or id(sentence) in self._guard_sent:
            return {}
        wanted = [a for a in (getattr(sentence, "actions", None) or [])
                  if isinstance(a, dict) and a.get("identifier") in GUARD_ACTIONS]
        if not wanted:
            return {}
        self._guard_sent.add(id(sentence))
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
            npc_id = guard.leader_for(npc_id, scene)
            result, issue = guard.action(action["identifier"], npc_id, scene)
            logger.log(23, f"Living World guard: {who.name} {action['identifier']}: {result}")
            if issue is not None and issue.key_value():
                keys[KEY_GUARD_ISSUE] = issue.key_value()
                keys[KEY_GUARD_ISSUE_IDS] = issue.form_ids()
                keys[KEY_GUARD_ISSUE_PLUGINS] = issue.plugins()
        return keys

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
    def _mood_seen(self, character: Any, mood: "moods.Mood") -> None:
        now = self._context.game_days
        npc_id = self._npc_ids.get(character.ref_id) or self._register(character, now)
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
            keys.update(self._guard_actions(sentence))
        except Exception:
            self.errors += 1
            logger.exception("Living World: a guard action failed; carrying on without it")
        text = self._moods_to_send.pop(speaker.ref_id, None)
        if text:
            keys[KEY_MOOD] = text
        return keys

    def start_reply_keys(self) -> dict[str, Any]:
        """Extra keys for the conversation start reply."""
        keys: dict[str, Any] = {KEY_QUIET_CHATTER: True} if self.quiet_chatter else {}
        wanting = self._wants_a_word()
        if wanting:
            keys[KEY_WANTS_A_WORD] = wanting
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

    def _noticed(self, npc_id: int, name: str, now: float, present: dict[int, str]) -> list[str]:
        try:
            with self.store.transaction() as db:
                return awareness.lines_for(db, npc_id, name, now, present, self._attire_now, hygiene=self.hygiene)
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
            if self.start_workers:
                worker.start()
        return self._stores[world_id]

    def _run_job(self, store: WorldStore, kind: str, payload: dict[str, Any]) -> None:
        if kind != EXTRACT_JOB:
            raise ValueError(f"unknown job kind {kind}")
        if self.complete is None:
            raise RuntimeError("no LLM attached for extraction")
        Extractor(store, self.complete).extract_conversation(payload["conv_id"])

    def _close(self, game_days: float | None) -> None:
        if game_days is None:
            game_days = self.store.scalar(
                "SELECT COALESCE(MAX(game_time), (SELECT started_gt FROM conversation WHERE conv_id = ?))"
                " FROM utterance WHERE conv_id = ?", (self.conv_id, self.conv_id))
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
            for world_id, store in self._stores.items():
                if store is self.store:
                    self._workers[world_id].notify()
        self.conv_id = None
        self._context = None

    def _sync(self, context: Any) -> None:
        log = ConversationLog(self.store)
        now_present: set[int] = set()
        for character in context.npcs_in_conversation.get_all_characters():
            npc_id = self._npc_ids.get(character.ref_id) or self._register(character, context.game_days)
            now_present.add(npc_id)
            log.add_participant(self.conv_id, npc_id)
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
