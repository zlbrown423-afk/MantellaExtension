import json
from typing import Any, Hashable

from fastapi import FastAPI, Request
from src.config.config_loader import ConfigLoader
from src.games.fallout4 import Fallout4
from src.games.gameable import Gameable
from src.games.skyrim import Skyrim
from src.output_manager import ChatManager
from src.llm.llm_client import LLMClient
from src.llm.summary_client import SummaryLLMClient
from src.game_manager import GameStateManager
from src.http.routes.routeable import routeable
from src.http.communication_constants import communication_constants as comm_consts
from src.tts.ttsable import TTSable
from src.tts.tts_factory import create_tts
from src import utils
from src.config.definitions.game_definitions import GameEnum
from src.config.definitions.tts_definitions import TTSEnum
from src.actions.function_manager import FunctionManager
import threading
import time
from starlette.concurrency import run_in_threadpool  # LIVINGWORLD PATCH
from livingworld.harness.recorder import TrafficRecorder  # LIVINGWORLD PATCH: optional traffic recording
from livingworld.perf.stats import STATS as PERF  # LIVINGWORLD PATCH: request timing

logger = utils.get_logger()


class mantella_route(routeable):
    """Main route for Mantella conversations

    Args:
        routeable (_type_): _description_
    """
    def __init__(self, config: ConfigLoader, language_info: dict[Hashable, str]) -> None:
        super().__init__(config)
        self.__language_info: dict[Hashable, str] = language_info
        self.__game: GameStateManager | None = None
        self.__request_lock = threading.Lock()  # LIVINGWORLD PATCH: see __handle_request
        # LIVINGWORLD PATCH: record /mantella traffic when MANTELLA_RECORD_TRAFFIC is set (off by default)
        self.__recorder: TrafficRecorder | None = TrafficRecorder.from_environment(config.save_folder)
        if self.__recorder:
            logger.info(f"Recording game traffic to {self.__recorder.file_path}")

        # if not self._can_route_be_used():
        #     error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
        #     logger.error(error_message)

    @utils.time_it
    def _setup_route(self):
        if self.__game:
            self.__game.end_conversation({})

        game: Gameable
        game_enum = self._config.game
        if game_enum.base_game == GameEnum.FALLOUT4:
            game = Fallout4(self._config)
        else:
            game = Skyrim(self._config)

        tts: TTSable = create_tts(self._config.tts_service, self._config, game)

        llm_client = LLMClient(self._config)

        summary_client: SummaryLLMClient | None = None
        if self._config.summary_llm_enabled:
            summary_client = SummaryLLMClient(self._config)

        chat_manager = ChatManager(self._config, tts, llm_client, game)
        self.__game = GameStateManager(game, chat_manager, self._config, self.__language_info, llm_client, summary_client)

    def __handle_request(self, request_type: str, received_json: dict[str, Any]) -> dict[str, Any]:
        """LIVINGWORLD PATCH: one request, on a worker thread, never two at once."""
        if request_type == comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION:
            # a player input request may only start waiting for speech after the first nudge: keep nudging
            while not self.__request_lock.acquire(timeout=0.5):
                self.__game.abort_player_wait()
        else:
            self.__request_lock.acquire()
        try:
            match request_type:
                case comm_consts.KEY_REQUESTTYPE_INIT:
                    # nothing needs to be done for this request aside from self._can_route_be_used() being triggered
                    logger.debug('Mantella settings initialized')
                    reply = {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTTYPE_INITCOMPLETED}
                case comm_consts.KEY_REQUESTTYPE_STARTCONVERSATION:
                    reply = self.__game.start_conversation(received_json)
                case comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION:
                    reply = self.__game.continue_conversation(received_json)
                case comm_consts.KEY_REQUESTTYPE_PLAYERINPUT:
                    reply = self.__game.player_input(received_json)
                case comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION:
                    reply = self.__game.end_conversation(received_json)
                case _:
                    reply = self.error_message(f"Request type '{request_type}' was not recognized")
        finally:
            self.__request_lock.release()
        return reply

    @utils.time_it
    def add_route_to_server(self, app: FastAPI):
        @app.post("/mantella")
        async def mantella(request: Request):
            if not self._can_route_be_used():
                error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
                logger.error(error_message)
                return self.error_message(error_message)
            if not self.__game:
                error_message = "Game manager setup failed. There is most likely an issue with the config.ini."
                logger.error(error_message)
                return self.error_message(error_message)
            reply = {}
            request_start = time.perf_counter()  # LIVINGWORLD PATCH
            received_json: dict[str, Any] | None = await request.json()
            if received_json:
                logger.debug('Processing request...')
                if self._config.show_http_debug_messages:
                    logger.log(self._log_level_http_in, json.dumps(received_json, indent=4))
                request_type: str = received_json[comm_consts.KEY_REQUESTTYPE]
                # LIVINGWORLD PATCH: requests are handled off the event loop, one at a time. A player input request
                # can block for minutes waiting for speech; handled on the loop it also blocked the game's end request,
                # so walking away or the end spell did nothing until the player spoke again. See __handle_request.
                reply = await run_in_threadpool(self.__handle_request, request_type, received_json)
            else:
                reply = self.error_message(f"Request did not contain properly formatted json!")

            if self._config.show_http_debug_messages:
                logger.log(self._log_level_http_out, json.dumps(reply, indent=4))
            if received_json:  # LIVINGWORLD PATCH: request timing for the performance monitor
                PERF.request_done(str(received_json.get(comm_consts.KEY_REQUESTTYPE, "?")), (time.perf_counter() - request_start) * 1000)
            if self.__recorder:  # LIVINGWORLD PATCH
                try:
                    self.__recorder.record(received_json, reply, (time.perf_counter() - request_start) * 1000)
                except Exception as e:
                    logger.warning(f"Could not record game traffic: {e}")
            return reply
