"""Drives Mantella's /mantella endpoint the way the game does, without the game.

``HeadlessSession`` starts the real Mantella route in-process with a scripted LLM
and a stub TTS, then plays the same request loop as ``MantellaConversation.psc``:
start, keep asking to continue until it is the player's turn, send player input,
and end the conversation. Nothing is sent to an API and no audio is played.

Example::

    with HeadlessSession(tmp_dir, ScriptedLLM(replies=["Welcome to Riverwood."])) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood")
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.end()
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.actions.function_manager import FunctionManager
from src.config.config_loader import ConfigLoader
from src.config.definitions.game_definitions import GameEnum
from src.http.communication_constants import communication_constants as comm_consts
from src.http.routes import mantella_route as mantella_route_module
from src.http.routes.mantella_route import mantella_route

from livingworld.harness.recorder import load_recording
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.stub_tts import StubTTS

ENGLISH = {"alpha2": "en", "language": "English", "hello": "Hello"}

# Settings that keep a headless run offline, silent and fast
HEADLESS_SETTINGS: dict[str, Any] = {
    "lip_generation": "Disabled",
    "fast_response_mode": False,
    "auto_launch_ui": False,
    "play_startup_sound": False,
    "vision_enabled": False,
    "summary_llm_enabled": False,
    "random_llm_enabled": False,
    "advanced_actions_enabled": False,
}


@dataclass
class SpokenLine:
    speaker: str
    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)


class HeadlessSession:
    def __init__(self, work_dir: str, llm: ScriptedLLM | None = None, settings: dict[str, Any] | None = None) -> None:
        self.work_dir = os.path.abspath(work_dir)
        self.llm = llm or ScriptedLLM()
        self.lines: list[SpokenLine] = []
        self.exchanges: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._patches: list[Any] = []
        self.config = self._build_config({**HEADLESS_SETTINGS, **(settings or {})})
        self.tts = StubTTS(self.config)

    # --- setup ----------------------------------------------------------------
    def _build_config(self, settings: dict[str, Any]) -> ConfigLoader:
        mod_folder = os.path.join(self.work_dir, "mod")
        os.makedirs(os.path.join(mod_folder, "Sound", "Voice", "Mantella.esp"), exist_ok=True)
        config = ConfigLoader(mygame_folder_path=self.work_dir, game_override=GameEnum.SKYRIM)
        definitions = config._ConfigLoader__definitions
        for key, value in {**settings, "skyrim_mod_folder": mod_folder}.items():
            try:
                definitions.get_config_value_definition(key).value = value
            except Exception:
                setattr(config, key, value)
        config.update_config_loader_with_changed_config_values()
        FunctionManager.load_all_actions(disabled_actions=config.disabled_actions)
        config.actions = FunctionManager.get_legacy_actions()
        return config

    def __enter__(self) -> "HeadlessSession":
        self._patches = [
            patch.object(mantella_route_module, "LLMClient", lambda config: self.llm),
            patch.object(mantella_route_module, "create_tts", lambda service, config, game=None: self.tts),
        ]
        for p in self._patches:
            p.start()
        self.route = mantella_route(config=self.config, language_info=ENGLISH)
        app = FastAPI()
        self.route.add_route_to_server(app)
        self.client = TestClient(app)
        return self

    def __exit__(self, *exc: Any) -> None:
        from livingworld.integration import LivingWorld
        if LivingWorld.latest is not None:
            LivingWorld.latest.stop()   # background workers end with the session
        for p in reversed(self._patches):
            p.stop()
        self._patches = []

    # --- raw requests ---------------------------------------------------------
    def post(self, request: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/mantella", json=request)
        response.raise_for_status()
        reply = response.json()
        self.exchanges.append((request, reply))
        if reply.get(comm_consts.KEY_REPLYTYPE) == "error":
            raise RuntimeError(f"Mantella returned an error: {reply.get('mantella_message')}")
        return reply

    # --- the game's conversation loop -----------------------------------------
    def start(self, actors: list[dict[str, Any]], location: str = "Riverwood", time: int = 12,
              game_days: float = 1.0, world_id: str = "HeadlessTest", events: list[str] | None = None) -> dict[str, Any]:
        self.post({comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_INIT})
        return self.post({
            comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_STARTCONVERSATION,
            comm_consts.KEY_ACTORS: actors,
            comm_consts.KEY_CONTEXT: self._context(location, time, game_days, events),
            comm_consts.KEY_INPUTTYPE: comm_consts.KEY_INPUTTYPE_TEXT,
            comm_consts.KEY_STARTCONVERSATION_WORLDID: world_id,
        })

    def continue_until_player_turn(self, actors: list[dict[str, Any]] | None = None, max_steps: int = 60) -> list[SpokenLine]:
        """Sends continue requests until Mantella hands the turn to the player or ends. Returns the new lines."""
        new_lines: list[SpokenLine] = []
        topic = 1
        for step in range(max_steps):
            request: dict[str, Any] = {
                comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
                comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: topic,
            }
            if actors is not None and step == 0:
                request[comm_consts.KEY_ACTORS] = actors
            reply = self.post(request)
            topic = 2 if topic == 1 else 1  # the game alternates between two dialogue lines
            reply_type = reply.get(comm_consts.KEY_REPLYTYPE)
            if reply_type == comm_consts.KEY_REPLYTYPE_NPCTALK:
                talk = reply.get(comm_consts.KEY_REPLYTYPE_NPCTALK)
                if talk:
                    line = SpokenLine(talk[comm_consts.KEY_ACTOR_SPEAKER], talk[comm_consts.KEY_ACTOR_LINETOSPEAK].strip(), talk.get(comm_consts.KEY_ACTOR_ACTIONS, []))
                    self.lines.append(line)
                    new_lines.append(line)
                    if {"identifier": comm_consts.ACTION_ENDCONVERSATION} in line.actions:
                        return new_lines
            elif reply_type in (comm_consts.KEY_REPLYTYPE_PLAYERTALK, comm_consts.KEY_REPLYTYPE_ENDCONVERSATION):
                return new_lines
            # npc_action and interrupted replies: the game just continues
        raise RuntimeError(f"No player turn after {max_steps} continue requests")

    def say(self, text: str, game_days: float | None = None) -> list[SpokenLine]:
        """Sends the player's line, then plays the NPC responses until it is the player's turn again."""
        request: dict[str, Any] = {
            comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_PLAYERINPUT,
            comm_consts.KEY_REQUESTTYPE_PLAYERINPUT: text,
        }
        if game_days is not None:
            request[comm_consts.KEY_CONTEXT] = {comm_consts.KEY_CONTEXT_GAMEDAYS: game_days}
        self.post(request)
        return self.continue_until_player_turn()

    def end(self, game_days: float | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION}
        if game_days is not None:
            request[comm_consts.KEY_ENDCONVERSATION_TIMESTAMP] = game_days
        return self.post(request)

    # --- replay ----------------------------------------------------------------
    def replay(self, recording_path: str) -> list[tuple[str, str]]:
        """Re-sends every recorded request in order.

        Returns (recorded reply type, replayed reply type) per request. With a scripted
        LLM the dialogue differs from the recording, so only the flow is comparable.
        """
        results: list[tuple[str, str]] = []
        for exchange in load_recording(recording_path):
            request = exchange.get("request")
            if not request:
                continue
            response = self.client.post("/mantella", json=request).json()
            self.exchanges.append((request, response))
            results.append((str(exchange["response"].get(comm_consts.KEY_REPLYTYPE)), str(response.get(comm_consts.KEY_REPLYTYPE))))
        return results

    # --- helpers ---------------------------------------------------------------
    @staticmethod
    def _context(location: str, time: int, game_days: float, events: list[str] | None) -> dict[str, Any]:
        return {
            comm_consts.KEY_CONTEXT_LOCATION: location,
            comm_consts.KEY_CONTEXT_TIME: time,
            comm_consts.KEY_CONTEXT_GAMEDAYS: game_days,
            comm_consts.KEY_CONTEXT_INGAMEEVENTS: events or [],
        }

    def lines_by(self, speaker: str) -> list[SpokenLine]:
        return [line for line in self.lines if line.speaker == speaker]
