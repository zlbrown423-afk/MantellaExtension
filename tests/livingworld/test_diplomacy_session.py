"""Diplomacy end to end through the headless Mantella session: the flag, the extraction, the prompt block, and
privacy between two leaders' conversations. The dialogue model is scripted, so this proves the wiring, not how a
real model argues."""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import jobs

BLOCK = "what Ulfric Stormcloak knows of the talks"


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def ulfric_talk(work_dir, day, say, extraction=None, location="Windhelm"):
    llm = ScriptedLLM(replies=["Speak plainly."],
                      rules=[when_player_says("truce", "Between us? A truce would save Nord lives.")],
                      extractions={"Ulfric Stormcloak": extraction or ScriptedExtraction()})
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Zachary"), actors.ulfric()], location=location, game_days=day)
        s.continue_until_player_turn()
        if say:
            s.say(say, game_days=day + 0.001)
        s.end(game_days=day + 0.01)
        LivingWorld.latest.wait_for_jobs()
    return llm


def test_off_by_default_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("MANTELLA_LW_DIPLOMACY", raising=False)
    llm = ulfric_talk(tmp_path, 3.0, "What of a truce with the Empire?")
    assert BLOCK not in llm.dialogue_calls[0].system_prompt
    assert all("diplomacy" not in c.system_prompt for c in llm.background_calls)
    with open_db(tmp_path) as db:
        assert db.execute("SELECT COUNT(*) FROM political_position").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dip_statement").fetchone()[0] == 0


def test_a_private_word_is_remembered_by_ulfric_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_DIPLOMACY", "1")
    said = ScriptedExtraction(memories=["Zachary asked me about a truce."], diplomacy=[
        {"phrase": "truce would save", "kind": "support", "issue": "ceasefire", "stance": 0.4,
         "summary": "thinks a truce would save Nord lives"}])
    first = ulfric_talk(tmp_path, 3.0, "What of a truce with the Empire?", said)
    prompt = first.dialogue_calls[0].system_prompt
    assert BLOCK in prompt and "Ulfric Stormcloak's own convictions" in prompt     # seeded on first meeting
    assert sum("diplomacy" in c.system_prompt for c in first.background_calls) == 1
    with open_db(tmp_path) as db:
        s = db.execute("SELECT * FROM dip_statement").fetchone()
        assert (s["kind"], s["audience"], s["modality"]) == ("support", "private", "asserted")
    again = ulfric_talk(tmp_path, 5.0, None)
    assert "thinks a truce would save Nord lives" in again.dialogue_calls[0].system_prompt
    # Tullius, in Solitude, knows nothing of it
    llm = ScriptedLLM(replies=["Yes?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Zachary"), actors.tullius()], location="Solitude", game_days=6.0)
        s.continue_until_player_turn()
        s.end(game_days=6.01)
        LivingWorld.latest.wait_for_jobs()
    tullius = llm.dialogue_calls[0].system_prompt
    assert "what General Tullius knows of the talks" in tullius
    assert "save Nord lives" not in tullius
