"""Raw speech recognition HUD (livingworld/dialogue/asr_hud.py): what the mic recognition heard is
reported once, unchanged, only for mic turns, and never changes what the NPCs get."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from livingworld.dialogue import asr_hud
from livingworld.integration import LivingWorld
from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts


# ---- formatting ---------------------------------------------------------------------------

def test_short_line_is_one_notification_with_the_words_unchanged():
    assert asr_hud.hud_lines("Helo Gerdur, hw are you?") == ['[Mic] "Helo Gerdur, hw are you?"']


def test_whitespace_is_collapsed_but_words_and_case_are_kept():
    assert asr_hud.hud_lines("  I  said\nTHANK you.  ") == ['[Mic] "I said THANK you."']


def test_nothing_to_show_for_empty_text():
    assert asr_hud.hud_lines("") == []
    assert asr_hud.hud_lines("   \n ") == []
    assert asr_hud.hud_keys(None) == {}
    assert asr_hud.hud_keys("") == {}


def test_long_line_is_split_into_numbered_parts_on_word_boundaries():
    words = ["word%02d" % i for i in range(25)]  # 25 x 6 chars + spaces = 174 chars
    lines = asr_hud.hud_lines(" ".join(words))
    assert len(lines) == 3
    assert lines[0].startswith('[Mic 1/3] "word00')
    assert lines[-1].endswith('word24"')
    body = " ".join(l.split("] ", 1)[1] for l in lines).replace('"', "")
    assert body.split() == words  # every word, in order, none changed
    for line in lines:
        assert len(line.split("] ", 1)[1].replace('"', "")) <= asr_hud.LINE_CHARS


def test_very_long_line_is_cut_after_max_lines():
    text = " ".join(["recognised"] * 60)
    lines = asr_hud.hud_lines(text)
    assert len(lines) == asr_hud.MAX_LINES
    assert lines[-1].endswith(' ..."')


def test_a_single_huge_word_is_hard_split():
    lines = asr_hud.hud_lines("a" * 150)
    assert len(lines) == 3
    assert "".join(l.split("] ", 1)[1].replace('"', "") for l in lines) == "a" * 150


@pytest.mark.parametrize("seconds, shows", [(1, 1), (4, 1), (4.5, 2), (8, 2), (12, 3), (60, 15)])
def test_shows_cover_the_display_time(seconds, shows):
    assert asr_hud.shows_for(seconds) == shows


def test_hud_keys_carry_lines_shows_and_gap():
    keys = asr_hud.hud_keys("hello", seconds=12)
    assert keys == {asr_hud.KEY_HEARD: ['[Mic] "hello"'], asr_hud.KEY_SHOWS: 3, asr_hud.KEY_GAP: asr_hud.GAP_SECONDS}


# ---- switches -----------------------------------------------------------------------------

def test_on_by_default_and_off_switch(monkeypatch):
    monkeypatch.delenv(asr_hud.HUD_ENV, raising=False)
    assert asr_hud.enabled()
    for off in ("0", "false", "OFF", " no "):
        monkeypatch.setenv(asr_hud.HUD_ENV, off)
        assert not asr_hud.enabled()
    monkeypatch.setenv(asr_hud.HUD_ENV, "1")
    assert asr_hud.enabled()


@pytest.mark.parametrize("raw, seconds", [("", 8.0), ("15", 15.0), ("0", 1.0), ("-3", 1.0), ("999", 60.0), ("abc", 8.0), ("nan", 8.0), ("2.5", 2.5)])
def test_display_seconds_from_environment(monkeypatch, raw, seconds):
    monkeypatch.setenv(asr_hud.SECONDS_ENV, raw)
    assert asr_hud.display_seconds() == seconds


def _living_world(asr_on: bool) -> LivingWorld:
    lw = LivingWorld.__new__(LivingWorld)  # only the switch matters here
    lw.enabled = True
    lw.errors = 0
    lw.asr_hud = asr_on
    return lw


def test_living_world_keys_respect_the_switch():
    assert _living_world(False).asr_hud_keys("hello") == {}
    assert _living_world(True).asr_hud_keys(None) == {}
    assert _living_world(True).asr_hud_keys("hello")[asr_hud.KEY_HEARD] == ['[Mic] "hello"']


def test_off_with_the_living_world(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTELLA_LIVING_WORLD", "0")
    monkeypatch.delenv(asr_hud.HUD_ENV, raising=False)
    lw = LivingWorld(str(tmp_path), enabled=False)
    assert lw.asr_hud is False
    assert lw.asr_hud_keys("hello") is None  # upstream Mantella replies stay as they were


def test_on_with_the_living_world_unless_switched_off(monkeypatch, tmp_path):
    monkeypatch.delenv(asr_hud.HUD_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    assert LivingWorld(str(tmp_path), enabled=True).asr_hud is True
    monkeypatch.setenv(asr_hud.HUD_ENV, "0")
    assert LivingWorld(str(tmp_path), enabled=True).asr_hud is False


# ---- capture in the conversation ----------------------------------------------------------

class FakeSTT:
    """Stands in for Transcriber: returns the queued results one call at a time."""

    def __init__(self, results: list[str | None]):
        self.results = list(results)
        self.is_listening = False
        self.prompts: list[str] = []

    def start_listening(self, prompt: str = "") -> None:
        self.is_listening = True
        self.prompts.append(prompt)

    def stop_listening(self) -> None:
        self.is_listening = False

    def get_latest_transcription(self, silence_timeout: float = 0) -> str | None:
        return self.results.pop(0)


def _mic_conversation(default_context, default_chat_manager, default_rememberer, llm_client, stt: FakeSTT, monkeypatch) -> Conversation:
    conv = Conversation(default_context, default_chat_manager, default_rememberer, llm_client, stt, True, False)
    monkeypatch.setattr(conv, "_Conversation__start_generating_npc_sentences", lambda: None)
    return conv


def _last_user_text(conv: Conversation) -> str:
    thread = conv._Conversation__messages
    return thread.get_last_message().text


def test_mic_line_is_captured_raw_and_reported_once(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    raw = "Helo Gerdur, hw are you?"
    conv = _mic_conversation(default_context, default_chat_manager, default_rememberer, llm_client, FakeSTT(["", raw]), monkeypatch)
    player_text, _, _ = conv.process_player_input("")
    assert player_text == raw                     # the pipeline's text is untouched
    assert _last_user_text(conv) == raw           # and so is what the NPCs get
    assert conv.pop_lw_heard() == raw
    assert conv.pop_lw_heard() is None            # reported once only


def test_typed_line_is_not_reported(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    stt = FakeSTT([])
    conv = _mic_conversation(default_context, default_chat_manager, default_rememberer, llm_client, stt, monkeypatch)
    conv.process_player_input("typed in the VR window")
    assert conv.pop_lw_heard() is None
    assert stt.prompts == []                      # the mic was not used


def test_silence_auto_response_is_not_reported(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    default_context.config.silence_auto_response_enabled = True
    default_context.config.silence_auto_response_max_count = 3
    conv = _mic_conversation(default_context, default_chat_manager, default_rememberer, llm_client, FakeSTT([None]), monkeypatch)
    player_text, _, _ = conv.process_player_input("")
    assert player_text == default_context.config.silence_auto_response_message
    assert conv.pop_lw_heard() is None


def test_a_new_turn_forgets_an_unreported_line(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    conv = _mic_conversation(default_context, default_chat_manager, default_rememberer, llm_client, FakeSTT(["first words"]), monkeypatch)
    conv.process_player_input("")
    conv.process_player_input("typed second line")
    assert conv.pop_lw_heard() is None


def test_stale_events_turn_still_reports_the_line(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    """When the wait was long, the server sends the text back for the game to re-send; the HUD
    shows it with that reply, and the re-sent (now typed-like) line is not reported again."""
    default_context.config.events_refresh_time = -1  # every wait counts as long
    conv = Conversation(default_context, default_chat_manager, default_rememberer, llm_client, FakeSTT(["late words"]), True, False)
    player_text, needs_update, _ = conv.process_player_input("")
    assert (player_text, needs_update) == ("late words", True)
    assert conv.pop_lw_heard() == "late words"


# ---- the reply to the game ----------------------------------------------------------------

def _manager_with(reply: dict[str, Any], heard: str | None, asr_on: bool, monkeypatch) -> GameStateManager:
    gm = GameStateManager.__new__(GameStateManager)
    talk = MagicMock()
    talk.pop_lw_heard.return_value = heard
    gm._GameStateManager__talk = talk
    gm._GameStateManager__living_world = _living_world(asr_on)
    monkeypatch.setattr(gm, "_GameStateManager__player_input", lambda input_json: dict(reply))
    return gm


@pytest.mark.parametrize("reply", [
    {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_NPCTALK},
    {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REQUESTTYPE_TTS, comm_consts.KEY_TRANSCRIBE: "late words"},
    {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_NPCACTION, comm_consts.KEY_REPLYTYPE_NPCACTION: {}},
])
def test_heard_line_rides_on_whatever_reply_goes_back(reply, monkeypatch):
    gm = _manager_with(reply, "late words", True, monkeypatch)
    out = gm.player_input({})
    assert out[asr_hud.KEY_HEARD] == ['[Mic] "late words"']
    assert {k: v for k, v in out.items() if not k.startswith("mantella_lw_asr_")} == reply  # nothing else changed


def test_reply_unchanged_when_off_or_nothing_heard(monkeypatch):
    reply = {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_NPCTALK}
    assert _manager_with(reply, "hello", False, monkeypatch).player_input({}) == reply
    assert _manager_with(reply, None, True, monkeypatch).player_input({}) == reply


def test_no_conversation_error_is_passed_through(monkeypatch):
    gm = GameStateManager.__new__(GameStateManager)
    gm._GameStateManager__talk = None
    gm._GameStateManager__living_world = _living_world(True)
    out = gm.player_input({})
    assert out[comm_consts.KEY_REPLYTYPE] == "error"
    assert asr_hud.KEY_HEARD not in out
