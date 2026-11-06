"""The bridge between upstream Mantella and the Living World store.

``GameStateManager`` calls these hooks at a few points (each marked ``LIVINGWORLD PATCH``).
The hooks only record; they never change what Mantella sends to the game, and any error inside
them is logged and swallowed so the store can never break a conversation.

Switched on with the environment variable ``MANTELLA_LIVING_WORLD=1``. Off by default until
Milestone 1 is complete.
"""
from __future__ import annotations

import functools
import os
import sqlite3
from typing import Any, Callable

from livingworld.store.conversation_log import ConversationLog
from livingworld.store.identity import ActorIdentity, FormId, IdentityRegistry, VoiceBindingConflict
from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

ENV_VAR = "MANTELLA_LIVING_WORLD"
_ON = {"1", "true", "yes", "on"}


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

    @classmethod
    def from_environment(cls, config: Any) -> "LivingWorld":
        enabled = os.environ.get(ENV_VAR, "").strip().lower() in _ON
        if enabled:
            logger.log(23, f"Living World store is on ({ENV_VAR}).")
        tts = getattr(config, "tts_service", None)
        return cls(config.save_folder, enabled, str(getattr(tts, "value", tts) or "unknown"))

    # ---- hooks ----
    @_never_raises
    def actors_seen(self, actor_jsons: list[dict[str, Any]] | None) -> None:
        """Keep the full form IDs from the request, before Mantella truncates them."""
        for actor_json in actor_jsons or []:
            identity = ActorIdentity.from_actor_json(actor_json)
            self._identities[identity.ref.mantella_id] = identity

    @_never_raises
    def conversation_started(self, world_id: str, context: Any) -> None:
        if self.conv_id is not None:
            self._close(context.game_days)
        self.store = self._store_for(world_id)
        npcs = context.npcs_in_conversation
        kind = "player" if npcs.contains_player_character() else "radiant"
        self.conv_id = ConversationLog(self.store).open(context.location, kind, context.game_days)
        self._context = context
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

    @_never_raises
    def conversation_ended(self, game_days: float | None, context: Any | None = None) -> None:
        if self.conv_id is None:
            return
        if game_days is None and context is not None:
            game_days = context.game_days
        self._close(game_days)

    # ---- internals ----
    def _store_for(self, world_id: str) -> WorldStore:
        if world_id not in self._stores:
            self._stores[world_id] = WorldStore.for_world(self.save_folder, world_id)
        return self._stores[world_id]

    def _close(self, game_days: float | None) -> None:
        if game_days is None:
            game_days = self.store.scalar(
                "SELECT COALESCE(MAX(game_time), (SELECT started_gt FROM conversation WHERE conv_id = ?))"
                " FROM utterance WHERE conv_id = ?", (self.conv_id, self.conv_id))
        ConversationLog(self.store).close(self.conv_id, game_days)
        logger.log(23, f"Living World: conversation {self.conv_id} closed at game day {game_days:.2f}")
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
                                     is_generic=character.is_generic_npc)
        if not character.is_player_character:
            try:
                registry.bind_voice(npc_id, character.tts_service or self.default_tts_service, character.tts_voice_model,
                                    character.in_game_voice_model, game_time)
            except VoiceBindingConflict as e:
                self.errors += 1
                logger.error(f"Living World: {character.name}: {e}")
        self._npc_ids[character.ref_id] = npc_id
        return npc_id

    @staticmethod
    def _identity_from_character(character: Any) -> ActorIdentity:
        # Only reached if the request's actor list was not seen first (should not happen in the game).
        base = FormId("unknown", "0x00" + character.base_id, character.base_id)
        ref = FormId("unknown", "0x00" + character.ref_id, character.ref_id)
        return ActorIdentity(character.name, base, ref, character.is_player_character)
