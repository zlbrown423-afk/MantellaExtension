import pytest
from src.config.config_loader import ConfigLoader
from src.llm.summary_client import SummaryLLMClient
from src.llm.client_base import ClientBase
from src.llm.llm_client import LLMClient
from src.remember.summaries import Summaries
from src.games.skyrim import Skyrim


class TestSummaryLLMClientInit:
    def test_init_uses_summary_config_values(self, default_config: ConfigLoader):
        """SummaryLLMClient should initialize with summary-specific config fields."""
        default_config.summary_llm_api = "OpenRouter"
        default_config.summary_llm = "google/gemma-4-26b-a4b-it:free"
        default_config.summary_custom_token_count = 8192

        client = SummaryLLMClient(default_config)
        assert client is not None
        assert isinstance(client, ClientBase)

    def test_summary_client_separate_from_main(self, default_config: ConfigLoader):
        """SummaryLLMClient should be a distinct instance from LLMClient."""
        main_client = LLMClient(default_config)
        summary_client = SummaryLLMClient(default_config)
        assert main_client is not summary_client


class TestFallbackToMainClient:
    def test_summaries_uses_main_client_when_no_summary_client(self, skyrim: Skyrim, default_config: ConfigLoader, llm_client: LLMClient, english_language_info):
        """When summary_client is None, Summaries should use the main client for summarization."""
        summaries = Summaries(skyrim, default_config, llm_client, english_language_info['language'], summary_client=None)
        assert summaries._Summaries__client is llm_client

    def test_summaries_uses_separate_client_when_provided(self, skyrim: Skyrim, default_config: ConfigLoader, llm_client: LLMClient, english_language_info):
        """When a summary_client is provided, Summaries should use it instead of the main client."""
        default_config.summary_llm_api = "OpenRouter"
        default_config.summary_llm = "google/gemma-4-26b-a4b-it:free"
        default_config.summary_custom_token_count = 8192
        summary_client = SummaryLLMClient(default_config)

        summaries = Summaries(skyrim, default_config, llm_client, english_language_info['language'], summary_client=summary_client)
        assert summaries._Summaries__client is summary_client


class TestConfigLoadingSummaryValues:
    def test_summary_config_values_exist(self, default_config: ConfigLoader):
        """Config should have summary-specific attributes."""
        assert hasattr(default_config, "summary_llm_enabled")
        assert hasattr(default_config, "summary_llm_api")
        assert hasattr(default_config, "summary_llm")
        assert hasattr(default_config, "summary_custom_token_count")
        assert hasattr(default_config, "summary_llm_params")
        assert hasattr(default_config, "conversation_summary_enabled")

    def test_summary_llm_enabled_default_false(self, default_config: ConfigLoader):
        """summary_llm_enabled should default to False (use main LLM for summaries by default)."""
        assert default_config.summary_llm_enabled is False

    def test_conversation_summary_enabled_default_true(self, default_config: ConfigLoader):
        """conversation_summary_enabled should default to True."""
        assert default_config.conversation_summary_enabled is True

    def test_summary_llm_has_default(self, default_config: ConfigLoader):
        """summary_llm should have a default model value."""
        assert default_config.summary_llm is not None
        assert len(default_config.summary_llm) > 0


# LIVINGWORLD PATCH: background-model fallback and summary sizing
class _FakeMain:
    model_name = "paid/conversation-model"
    token_limit = 128000

    def __init__(self):
        self.calls = 0

    def request_call(self, messages):
        self.calls += 1
        return "paid reply"


def _summary_client(default_config, monkeypatch, replies, fallback):
    default_config.summary_llm_api = "OpenRouter"
    default_config.summary_llm = "free/background-model:free"
    client = SummaryLLMClient(default_config, fallback=fallback)
    calls = []

    def fake_request(self, messages):
        calls.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(ClientBase, "request_call", fake_request)
    return client, calls


class TestBackgroundFallback:
    def test_free_reply_is_used(self, default_config, monkeypatch):
        main = _FakeMain()
        client, calls = _summary_client(default_config, monkeypatch, ["free reply"], main)
        assert client.request_call("msg") == "free reply"
        assert main.calls == 0 and client.fallback_calls == 0

    def test_no_reply_falls_back_then_skips_free_model(self, default_config, monkeypatch):
        main = _FakeMain()
        client, calls = _summary_client(default_config, monkeypatch, [None, "free reply"], main)
        assert client.request_call("msg") == "paid reply"
        assert client.request_call("msg") == "paid reply"   # cooling down: free model not tried
        assert len(calls) == 1 and main.calls == 2

    def test_free_model_retried_after_cooldown(self, default_config, monkeypatch):
        import src.llm.summary_client as summary_module
        main = _FakeMain()
        client, calls = _summary_client(default_config, monkeypatch, [None, "free reply"], main)
        now = [1000.0]
        monkeypatch.setattr(summary_module.time, "monotonic", lambda: now[0])
        assert client.request_call("msg") == "paid reply"
        now[0] += summary_module.FALLBACK_COOLDOWN_SECONDS + 1
        assert client.request_call("msg") == "free reply"

    def test_error_falls_back(self, default_config, monkeypatch):
        main = _FakeMain()
        client, _ = _summary_client(default_config, monkeypatch, [RuntimeError("404 model gone")], main)
        assert client.request_call("msg") == "paid reply"

    def test_fallback_can_be_turned_off(self, default_config, monkeypatch):
        monkeypatch.setenv("MANTELLA_LW_BACKGROUND_FALLBACK", "0")
        main = _FakeMain()
        client, _ = _summary_client(default_config, monkeypatch, [None], main)
        assert client.request_call("msg") is None
        assert main.calls == 0

    def test_without_fallback_errors_still_raise(self, default_config, monkeypatch):
        client, _ = _summary_client(default_config, monkeypatch, [RuntimeError("boom")], None)
        with pytest.raises(RuntimeError):
            client.request_call("msg")


class TestSummarySizing:
    def test_summary_budget_uses_conversation_model_limit(self, skyrim, default_config, llm_client, english_language_info):
        default_config.summary_llm_api = "OpenRouter"
        default_config.summary_llm = "free/unknown-model:free"
        default_config.summary_custom_token_count = 4096
        summary_client = SummaryLLMClient(default_config)
        summaries = Summaries(skyrim, default_config, llm_client, english_language_info['language'], summary_client=summary_client)
        assert summaries._Summaries__client is summary_client
        assert summaries._Summaries__prompt_client is llm_client
