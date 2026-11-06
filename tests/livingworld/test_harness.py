"""Milestone 0: the headless harness drives real Mantella conversations offline.

These tests pin down today's behaviour so later milestones can show what they change.
Tests marked ``baseline_behaviour`` document known identity-isolation problems in
upstream Mantella; Milestone 1 is expected to flip them.
"""
import json
import os

import pytest

from livingworld.harness import actors
from livingworld.harness.recorder import ENV_VAR, TrafficRecorder, load_recording
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from src.http.communication_constants import communication_constants as comm_consts


@pytest.fixture
def gerdur_llm() -> ScriptedLLM:
    return ScriptedLLM(
        replies=["Welcome to Riverwood, traveler."],
        rules=[
            when_player_says("how old", "I'm 31, not that it's any of your business."),
            when_player_says("favorite color", "Green. Like the pines around the mill."),
        ],
    )


def test_single_npc_conversation_runs_offline(tmp_path, gerdur_llm):
    with HeadlessSession(str(tmp_path), gerdur_llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood", game_days=3.5)
        greeting = s.continue_until_player_turn()
        answer = s.say("How old are you, Gerdur?")
        s.say("And your favorite color?")
        s.end(game_days=3.6)

    assert [line.speaker for line in greeting] == ["Gerdur"]
    assert "Welcome to Riverwood" in greeting[0].text
    assert "31" in " ".join(line.text for line in answer)
    assert all(line.speaker == "Gerdur" for line in s.lines)
    # every NPC line went through the scripted LLM, none through an API
    assert len(gerdur_llm.dialogue_calls) == 3


def test_prompts_are_captured_for_inspection(tmp_path, gerdur_llm):
    with HeadlessSession(str(tmp_path), gerdur_llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood")
        s.continue_until_player_turn()
        s.say("How old are you?")

    first = gerdur_llm.dialogue_calls[0]
    assert "Gerdur" in first.system_prompt
    assert "Riverwood" in first.system_prompt
    assert "How old are you?" in gerdur_llm.dialogue_calls[-1].last_user_text


def test_each_line_is_voiced_with_the_speakers_voice(tmp_path, gerdur_llm):
    with HeadlessSession(str(tmp_path), gerdur_llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood")
        s.continue_until_player_turn()
        s.say("How old are you?")

    voiced = [line for line in s.tts.lines if line.text]
    assert voiced, "expected synthesized lines"
    assert {line.voice for line in voiced} == {"FemaleNord"}


def test_memory_persists_to_disk_after_end(tmp_path, gerdur_llm):
    gerdur_llm.summary_reply = "Gerdur told the player she is 31 and likes green."
    with HeadlessSession(str(tmp_path), gerdur_llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood", game_days=2.0)
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.say("Favorite color?")
        s.end(game_days=2.1)

    conversations = tmp_path / "data" / "Skyrim" / "conversations" / "HeadlessTest"
    summaries = list(conversations.rglob("Gerdur_summary_*.txt"))
    logs = list(conversations.rglob("Gerdur.json"))
    assert len(summaries) == 1 and "31" in summaries[0].read_text(encoding="utf-8")
    assert len(logs) == 1 and json.loads(logs[0].read_text(encoding="utf-8"))


def test_group_conversation_routes_lines_by_name_prefix(tmp_path):
    llm = ScriptedLLM(replies=[
        "Gerdur: Hod, come meet our guest. Hod: Well met, friend.",
        "Hod: Aye, the mill keeps us busy. Gerdur: Too busy.",
    ])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player(), actors.gerdur(), actors.hod()], location="Riverwood")
        s.continue_until_player_turn()
        s.say("How is the mill?")

    assert [line.speaker for line in s.lines] == ["Gerdur", "Hod", "Hod", "Gerdur"]
    voices = {line.text: line.voice for line in s.tts.lines}
    assert voices["Well met, friend."] == "MaleNord"
    assert voices["Hod, come meet our guest."] == "FemaleNord"


@pytest.mark.baseline_behaviour
def test_baseline_group_prompt_holds_every_npcs_memories(tmp_path):
    """Upstream behaviour (audit risk R1): one prompt carries all NPCs' memories.

    Milestone 1 replaces this with per-speaker prompts; this test should then be inverted.
    """
    llm = ScriptedLLM(replies=["Welcome."], summary_reply="Gerdur privately told the player her secret: she is 31.")
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood", game_days=1.0)
        s.continue_until_player_turn()
        s.say("Tell me a secret.")
        s.say("Anything else?")
        s.end(game_days=1.1)

    group_llm = ScriptedLLM(replies=["Hod: Hello there. Gerdur: Hello again."])
    with HeadlessSession(str(tmp_path), group_llm) as s:
        s.start([actors.player(), actors.gerdur(), actors.hod()], location="Riverwood", game_days=8.0)
        s.continue_until_player_turn()

    prompt = group_llm.dialogue_calls[0].system_prompt
    assert "she is 31" in prompt  # Gerdur's memory...
    assert "Hod" in prompt         # ...sits in the same prompt that writes Hod's lines


def test_recorder_writes_and_reloads_exchanges(tmp_path):
    recorder = TrafficRecorder(str(tmp_path / "rec.jsonl"))
    recorder.record({"mantella_request_type": "mantella_initialize"}, {"mantella_reply_type": "mantella_init_completed"}, 1.5)
    exchanges = load_recording(str(tmp_path / "rec.jsonl"))
    assert len(exchanges) == 1
    assert exchanges[0]["duration_ms"] == 1.5
    assert exchanges[0]["request"]["mantella_request_type"] == "mantella_initialize"


def test_recorder_is_off_unless_switched_on(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert TrafficRecorder.from_environment(str(tmp_path)) is None
    monkeypatch.setenv(ENV_VAR, "0")
    assert TrafficRecorder.from_environment(str(tmp_path)) is None
    monkeypatch.setenv(ENV_VAR, "1")
    recorder = TrafficRecorder.from_environment(str(tmp_path))
    assert recorder is not None
    assert recorder.file_path.startswith(os.path.join(str(tmp_path), "data", "recordings"))


def test_recorded_session_replays_with_the_same_flow(tmp_path, monkeypatch, gerdur_llm):
    record_dir = tmp_path / "recordings"
    monkeypatch.setenv(ENV_VAR, str(record_dir))
    with HeadlessSession(str(tmp_path / "first"), gerdur_llm) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood")
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.end()
    monkeypatch.delenv(ENV_VAR)

    recording = next(record_dir.glob("*.jsonl"))
    exchanges = load_recording(str(recording))
    assert len(exchanges) == len(s.exchanges)
    assert all(e["duration_ms"] >= 0 for e in exchanges)

    replay_llm = ScriptedLLM(replies=["Welcome to Riverwood, traveler."], rules=gerdur_llm.rules)
    with HeadlessSession(str(tmp_path / "second"), replay_llm) as replayed:
        results = replayed.replay(str(recording))
    assert [recorded for recorded, _ in results] == [new for _, new in results]
    assert results[-1][0] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION


def test_latency_summary_measures_player_input_to_first_line(tmp_path):
    from livingworld.baseline.summarize_recording import first_line_latencies, summarize
    path = tmp_path / "rec.jsonl"
    rec = TrafficRecorder(str(path))
    entries = [
        {"kind": "exchange", "seq": 1, "t_ms": 1000.0, "duration_ms": 100.0,
         "request": {"mantella_request_type": "mantella_player_input"}, "response": {"mantella_reply_type": "mantella_npc_talk"}},
        {"kind": "exchange", "seq": 2, "t_ms": 1500.0, "duration_ms": 400.0,
         "request": {"mantella_request_type": "mantella_continue_conversation"}, "response": {"mantella_reply_type": "mantella_npc_talk"}},
        {"kind": "exchange", "seq": 3, "t_ms": 3900.0, "duration_ms": 2300.0,
         "request": {"mantella_request_type": "mantella_continue_conversation"},
         "response": {"mantella_reply_type": "mantella_npc_talk", "mantella_npc_talk": {"mantella_actor_speaker": "Gerdur"}}},
    ]
    for entry in entries:
        rec._write(entry)
    assert first_line_latencies(load_recording(str(path))) == [3000.0]
    assert "median=3.00s" in summarize(str(path))
