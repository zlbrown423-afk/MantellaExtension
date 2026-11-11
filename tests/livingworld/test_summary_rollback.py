"""Mantella's own summaries and transcripts roll back with the save (2026-11-11: Salonia remembered a Whiterun
conversation after the user reloaded a save from before it, from her Mantella summary file)."""
import json

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from src.conversation.conversation_log import conversation_log


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


def chat(work_dir, day, summary, extra, lines=("Hello.", "And?", "Go on.")):
    llm = ScriptedLLM(replies=["Aye."])
    llm.summary_reply = summary
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=day, extra=extra)
        s.continue_until_player_turn()
        for line in lines:
            s.say(line)
        s.end(game_days=day + 0.05)
    return llm, reply


def prompt_on(work_dir, day, extra):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=day, extra=extra)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.01)
    return llm.dialogue_calls[0].system_prompt, reply


def summary_file(work_dir):
    files = list((work_dir / "data" / "Skyrim" / "conversations" / "HeadlessTest").rglob("Gerdur_summary_*.txt"))
    assert len(files) == 1
    return files[0]


def test_a_summary_written_after_the_save_is_left_out_and_comes_back(tmp_path):
    chat(tmp_path, 3.5, "Torvar asked Gerdur about the mill.", load(0, 101, 3.4))
    chat(tmp_path, 6.0, "Torvar told Gerdur a secret about Whiterun.", load(1, 102, 5.9))
    assert "secret about Whiterun" in summary_file(tmp_path).read_text(encoding="utf-8")

    older, reply = prompt_on(tmp_path, 4.1, load(1, 201, 4.0))     # an older save, from before day 6
    assert reply[KEY_TIMELINE_ID] == 2
    assert "about the mill" in older and "secret about Whiterun" not in older
    assert "secret about Whiterun" in summary_file(tmp_path).read_text(encoding="utf-8")   # nothing deleted

    later, reply = prompt_on(tmp_path, 6.2, load(1, 202, 6.1))     # the later save again
    assert reply[KEY_TIMELINE_ID] == 1 and "secret about Whiterun" in later


def test_the_transcript_count_rolls_back_too(tmp_path):
    chat(tmp_path, 3.5, "First.", load(0, 101, 3.4))
    chat(tmp_path, 6.0, "Second.", load(1, 102, 5.9), lines=("About Whiterun.", "Really?", "Tell me more."))
    gerdur = type("C", (), {"name": "Gerdur", "ref_id": None})()
    folder = next((tmp_path / "data" / "Skyrim" / "conversations" / "HeadlessTest").glob("Gerdur*"))
    gerdur.ref_id = folder.name.split(" - ")[-1] if " - " in folder.name else ""
    both = conversation_log.get_conversation_log_length(gerdur, "HeadlessTest")
    prompt_on(tmp_path, 4.1, load(1, 201, 4.0))
    transcripts = json.loads(next(folder.glob("Gerdur.json")).read_text(encoding="utf-8"))
    after = conversation_log.get_conversation_log_length(gerdur, "HeadlessTest")
    # the older save's own conversation (just now) is kept; the day-6 one is not counted
    assert len(transcripts) == 3
    assert after == both - len(transcripts[1]) + len(transcripts[2])


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_SUMMARY_ROLLBACK", "0")
    chat(tmp_path, 3.5, "Torvar asked Gerdur about the mill.", load(0, 101, 3.4))
    chat(tmp_path, 6.0, "Torvar told Gerdur a secret about Whiterun.", load(1, 102, 5.9))
    older, _ = prompt_on(tmp_path, 4.1, load(1, 201, 4.0))
    assert "secret about Whiterun" in older
