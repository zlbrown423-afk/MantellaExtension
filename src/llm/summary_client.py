import os
import time

from src.config.config_loader import ConfigLoader
from src.llm.client_base import ClientBase
from src.llm.messages import Message
from src.llm.message_thread import message_thread
from src.model_profile_manager import get_profile_manager
import src.utils as utils

logger = utils.get_logger()

# LIVINGWORLD PATCH: background-model fallback.
# The summary client also serves Living World's memory extraction, so it is the place to put a free
# model (``Custom Summary Model``). Free models get rate limited (20/min, 50 or 1000 a day on OpenRouter)
# and come and go, so a failed call is sent once more on the conversation model instead of being lost.
# After a failure the background model is skipped for a while, so a spent daily cap doesn't cost a
# 5-second wait on every call. Turn the fallback off with MANTELLA_LW_BACKGROUND_FALLBACK=0.
FALLBACK_ENV_VAR = "MANTELLA_LW_BACKGROUND_FALLBACK"
FALLBACK_COOLDOWN_SECONDS = 600


def fallback_enabled() -> bool:
    return os.environ.get(FALLBACK_ENV_VAR, "1").strip().lower() not in ("0", "false", "no", "off")


class SummaryLLMClient(ClientBase):
    '''LLM client dedicated to generating conversation summaries.'''
    def __init__(self, config: ConfigLoader, fallback: ClientBase | None = None) -> None:
        profile_manager = get_profile_manager()
        summary_llm_params = profile_manager.resolve_params(
            service=config.summary_llm_api,
            model=config.summary_llm,
            fallback_params=config.summary_llm_params,
            apply_profile=config.apply_model_profiles,
            log_context="SummaryLLMClient",
        )
        super().__init__(config.summary_llm_api, config.summary_llm, summary_llm_params, config.summary_custom_token_count)
        self._fallback: ClientBase | None = fallback if fallback_enabled() else None
        self._skip_until: float = 0.0
        self.fallback_calls: int = 0

    def request_call(self, messages: Message | message_thread) -> str | None:
        if self._fallback is None:
            return super().request_call(messages)
        if time.monotonic() >= self._skip_until:
            try:
                reply = super().request_call(messages)
            except Exception as e:  # bad gateway, model withdrawn, key without free access...
                logger.warning(f"Background model {self.model_name} failed ({type(e).__name__}: {e})")
                reply = None
            if reply:
                return reply
            self._skip_until = time.monotonic() + FALLBACK_COOLDOWN_SECONDS
            logger.warning(f"Background model {self.model_name} gave no reply; using {self._fallback.model_name} "
                           f"for background jobs for the next {FALLBACK_COOLDOWN_SECONDS // 60} minutes")
        self.fallback_calls += 1
        return self._fallback.request_call(messages)
