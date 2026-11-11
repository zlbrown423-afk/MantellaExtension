"""Salonia Carvain begs for her life once her husband and bodyguard are dead, and then plays the player's wingman.

The game's ``react`` record (``via=surrender``) is driven through the real code path, as Mantella-Spell writes it.
"""
import json
import sqlite3

import pytest

from livingworld import mood as moods
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, actor_field, react
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perception.records import BadRecord, parse_record
from livingworld.social import surrender, wingman

PLAYER = actors.player("Torvar")
SALONIA = actors.actor("Salonia Carvain", 0x000B8149, 0xFF001A01, 1, "Imperial", "FemaleCommoner")
PLAUTIS = actors.actor("Plautis Carvain", 0x000B8148, 0xFF00196B, 0, "Imperial", "MaleCommoner")
BODYGUARD = actors.actor("Bodyguard", 0x000B814A, 0xFF001A02, 0, "Nord", "MaleNord")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BB0, 1, "Nord", "FemaleNord")
GUARD = actors.actor("Whiterun Guard", 0x0001A6A3, 0x000A0A44, 0, "Nord", "MaleNord")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def surrendered(day, killer="player", bodyguard=True):
    rec = react(SALONIA, PLAYER, day, via="surrender", victim_json=PLAUTIS, location="Haafingar")
    if bodyguard:
        rec += f"|d={actor_field(BODYGUARD)}"
    return rec + f"|killer={killer}"


def talk(work_dir, npcs, day, events=None):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        start = s.start([PLAYER, *npcs], game_days=day, context_extra={KEY_EVENTS: events or []})
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt, start


def test_the_record_carries_the_dead_and_the_killer():
    record = parse_record(surrendered(3.5))
    assert record.via == "surrender" and record.party.name == "Plautis Carvain"
    assert [a.name for a in record.dead] == ["Bodyguard"] and record.killer == "player"


def test_a_surrender_needs_the_husband():
    with pytest.raises(BadRecord):
        parse_record(react(SALONIA, PLAYER, 3.5, via="surrender"))


def test_salonia_begs_remembers_and_is_terrified(tmp_path):
    prompt, start = talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    assert start[surrender.KEY_SURRENDER_ON] is True
    assert "Torvar has just killed Plautis Carvain and Bodyguard, right in front of Salonia Carvain" in prompt
    assert "begs Torvar to spare Salonia Carvain" in prompt and "use Follow" in prompt and "Never use Attack" in prompt
    db = open_db(tmp_path)
    me = npc_id(db, "Salonia Carvain")
    memory = db.execute("SELECT content, importance FROM memory WHERE owner_id = ?", (me,)).fetchone()
    assert "Torvar killed Plautis Carvain and Bodyguard in front of me at Haafingar" in memory["content"]
    assert memory["importance"] == 1.0
    event = db.execute("SELECT payload_json FROM world_event WHERE type = 'reaction'").fetchone()
    payload = json.loads(event[0])
    assert payload["killer"] == "player" and len(payload["dead"]) == 2


def test_later_she_is_wary_not_begging(tmp_path):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    later, _ = talk(tmp_path, [SALONIA], 4.5)
    assert "has not forgotten that Torvar killed Plautis Carvain and Bodyguard" in later
    assert "begs Torvar to spare" not in later


def test_someone_else_killed_them(tmp_path):
    prompt, _ = talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995, killer="other", bodyguard=False)])
    assert "Plautis Carvain is dead, and Salonia Carvain is left alone and helpless" in prompt


def test_surrender_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(surrender.ENV, "0")
    prompt, start = talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    assert start[surrender.KEY_SURRENDER_ON] is False and "spare" not in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event WHERE type = 'reaction'").fetchone()[0] == 0


# ---- wingman ----------------------------------------------------------------------------------------

def set_mood(work_dir, name, score, day):
    db = open_db(work_dir)
    moods.save(db, npc_id(db, name), moods.Mood("warm" if score >= 7 else "cold", score), day)
    db.commit()


def test_wingman_goes_bold_when_she_is_receptive_and_alone(tmp_path):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    talk(tmp_path, [YSOLDA], 4.0)
    set_mood(tmp_path, "Ysolda", 8, 4.1)
    prompt, _ = talk(tmp_path, [SALONIA, YSOLDA], 4.1)
    assert "Salonia Carvain has made it Salonia Carvain's business to be Torvar's wingman" in prompt
    assert "even lewd if Ysolda plays along" in prompt


def test_wingman_backs_off_when_she_is_cold(tmp_path):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    talk(tmp_path, [YSOLDA], 4.0)
    set_mood(tmp_path, "Ysolda", 2, 4.1)
    prompt, _ = talk(tmp_path, [SALONIA, YSOLDA], 4.1)
    assert "backs off" in prompt and "lewd" not in prompt


def test_wingman_keeps_it_clean_in_front_of_a_guard(tmp_path):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    talk(tmp_path, [YSOLDA], 4.0)
    set_mood(tmp_path, "Ysolda", 8, 4.1)
    db = open_db(tmp_path)
    talk(tmp_path, [GUARD], 4.05)
    guard_id = npc_id(db, "Whiterun Guard")
    present = {npc_id(db, "Salonia Carvain"): "Salonia Carvain", npc_id(db, "Ysolda"): "Ysolda",
               guard_id: "Whiterun Guard", npc_id(db, "Torvar"): "Torvar"}
    line = wingman.line(db, npc_id(db, "Salonia Carvain"), "Salonia Carvain", npc_id(db, "Torvar"), "Torvar",
                        present, {guard_id}, 4.1)
    assert "A guard is listening" in line and "lewd" not in line


def test_nobody_else_is_a_wingman(tmp_path):
    talk(tmp_path, [YSOLDA], 4.0)
    db = open_db(tmp_path)
    assert wingman.line(db, npc_id(db, "Ysolda"), "Ysolda", npc_id(db, "Torvar"), "Torvar", {}, set(), 4.0) is None


def test_wingman_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(wingman.ENV, "0")
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    prompt, _ = talk(tmp_path, [SALONIA, YSOLDA], 4.1)
    assert "wingman" not in prompt


# ---- her "I'll come along" makes her follow (2026-11-11: the model never called Follow) -------------------

from livingworld.harness.scripted_llm import when_player_says  # noqa: E402
from src.actions.function_manager import FunctionManager  # noqa: E402


@pytest.fixture
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


@pytest.mark.parametrize("line", ["Thank you, thank you. I'll come along, I swear I'll be useful.",
                                  "I'll follow you anywhere.", "I'm right behind you.", "I will stay close to you.",
                                  "Lead the way."])
def test_accepting(line):
    assert surrender.accepts(line)


@pytest.mark.parametrize("line", ["Please, let me come with you!", "May I come with you?", "I'll come along?",
                                  "I won't follow you.", "I'll come with you once I've buried him.",
                                  "Spare me, I beg you."])
def test_not_accepting(line):
    assert not surrender.accepts(line)


def test_the_watcher_adds_follow_once_and_not_after_a_no():
    pending = {"Salonia Carvain"}
    w = surrender.FollowWatcher(pending, "Fine, you can come.", {surrender.FOLLOW})
    acts = []
    assert w.keep("Salonia Carvain", "I'll come along.", acts) == [surrender.FOLLOW]
    assert acts == [{"identifier": surrender.FOLLOW, "arguments": {"source": ["Salonia Carvain"]}}]
    assert w.keep("Salonia Carvain", "I'll follow you.", []) == [] and not pending
    refused = surrender.FollowWatcher({"Salonia Carvain"}, "No. Go away.", {surrender.FOLLOW})
    assert refused.keep("Salonia Carvain", "I'll come along.", []) == []


def test_in_game_her_yes_carries_follow(tmp_path, reset_actions):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)])
    llm = ScriptedLLM(rules=[when_player_says("come", "Thank you. I'll come along, I promise I'll be useful.")],
                      replies=["Please, spare me."])
    with HeadlessSession(str(tmp_path), llm, settings={"disabled_actions": ""}) as s:
        s.start([PLAYER, SALONIA], game_days=3.5005)
        s.continue_until_player_turn()
        said = s.say("Alright, you can come with me.")
        s.end(game_days=3.502)
    sent = [a for line in said for a in line.actions]
    assert {"identifier": surrender.FOLLOW, "arguments": {"source": ["Salonia Carvain"]}} in sent
