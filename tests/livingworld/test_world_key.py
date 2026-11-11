"""Renaming the character keeps the same world (database and Mantella summaries).

Found in game 2026-11-11: a new game ran as "Adventurer" for its first twenty minutes, then the
player set the name "Zachary Lex". Mantella names worlds after the player, so Alfhild, Ria and
Nimriel lost everything said before the rename. The game now keeps a world key in the save.
"""
import os
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from livingworld.store import worlds
from livingworld.store.worlds import KEY_WORLD_KEY, clean_key, resolve_world


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def db_path(folder, world):
    return folder / "data" / "livingworld" / f"{world}.sqlite"


def make_world(folder, world):
    path = db_path(folder, world)
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()


def talk(folder, player, world_id, extra, day, llm=None):
    llm = llm or ScriptedLLM(replies=["Well met."])
    with HeadlessSession(str(folder), llm) as s:
        reply = s.start([actors.player(player), actors.gerdur()], game_days=day, world_id=world_id, extra=extra)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.05)
        LivingWorld.latest.wait_for_jobs()
    return reply, llm


# ---- the rules ----

def test_older_game_scripts_keep_the_name_based_world(tmp_path):
    make_world(tmp_path, "Adventurer1")
    assert resolve_world(str(tmp_path), "Adventurer1", {}) == "Adventurer1"


def test_a_key_in_the_save_wins_over_the_name(tmp_path):
    assert resolve_world(str(tmp_path), "ZacharyLex1", {KEY_WORLD_KEY: "Adventurer1"}) == "Adventurer1"


def test_a_new_game_gets_its_own_world_when_the_default_name_is_taken(tmp_path):
    make_world(tmp_path, "Adventurer1")
    (tmp_path / "data" / "Skyrim" / "conversations" / "Adventurer1-2").mkdir(parents=True)
    new_game = {KEY_WORLD_KEY: ""}  # no save loaded yet, so no load nonce
    assert resolve_world(str(tmp_path), "Adventurer1", new_game) == "Adventurer1-3"
    assert resolve_world(str(tmp_path), "Nobody1", new_game) == "Nobody1"


def test_a_loaded_save_without_a_key_keeps_the_name_based_world(tmp_path):
    """Saves from before keys existed, and saves whose MCM World ID was just changed (the game clears the key)."""
    make_world(tmp_path, "ZacharyLex1")
    loaded = {KEY_WORLD_KEY: "", KEY_LOAD_NONCE: 77}
    assert resolve_world(str(tmp_path), "ZacharyLex1", loaded) == "ZacharyLex1"


def test_unsafe_keys_are_ignored():
    assert clean_key("../../evil") == "evil"
    assert clean_key("Zachary Lex1") == "ZacharyLex1"
    assert clean_key("-x") == ""
    assert clean_key(None) == ""
    assert clean_key("a" * 81) == ""


# ---- end to end ----

def test_renaming_the_player_keeps_the_npcs_memories(tmp_path):
    first = ScriptedLLM(replies=["You'd help with the harvest? Bless you."],
                        extractions={"Gerdur": ScriptedExtraction(
                            facts=[Fact("help with the harvest", "Gerdur", "asked_for", "harvest help", "Gerdur")])})
    reply, _ = talk(tmp_path, "Adventurer", "Adventurer1", {KEY_WORLD_KEY: ""}, 1.4, first)
    assert reply[KEY_WORLD_KEY] == "Adventurer1"   # the game stores this in the save

    # later the player names the character; the save still carries the key
    reply, llm = talk(tmp_path, "Zachary Lex", "ZacharyLex1",
                      {KEY_WORLD_KEY: reply[KEY_WORLD_KEY], KEY_LOAD_NONCE: 5, KEY_LOADED_GAME_DAYS: 1.6,
                       KEY_TIMELINE_ID: 0}, 1.8)
    assert reply[KEY_WORLD_KEY] == "Adventurer1"
    assert reply[KEY_TIMELINE_ID] == 1
    assert not db_path(tmp_path, "ZacharyLex1").exists()
    db = sqlite3.connect(db_path(tmp_path, "Adventurer1"))
    assert db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM npc WHERE is_player = 1").fetchone()[0] == 1
    assert "harvest" in llm.dialogue_calls[0].system_prompt

    # Mantella's own summaries stay in one folder too
    folders = {name for name in os.listdir(tmp_path / "data")
               if os.path.isdir(tmp_path / "data" / name / "conversations")}
    for game_folder in folders:
        assert not (tmp_path / "data" / game_folder / "conversations" / "ZacharyLex1").exists()


def test_a_second_new_game_does_not_inherit_the_first_games_default_world(tmp_path):
    talk(tmp_path, "Adventurer", "Adventurer1", {KEY_WORLD_KEY: ""}, 1.4)
    reply, _ = talk(tmp_path, "Adventurer", "Adventurer1", {KEY_WORLD_KEY: ""}, 1.35)
    assert reply[KEY_WORLD_KEY] == "Adventurer1-2"
    assert db_path(tmp_path, "Adventurer1-2").exists()


def test_diary_requests_follow_the_resolved_world(tmp_path):
    world = LivingWorld(str(tmp_path))
    assert world.resolve_world("ZacharyLex1", {KEY_WORLD_KEY: "Adventurer1"}) == "Adventurer1"
    assert world.world_for_name("ZacharyLex1") == "Adventurer1"
    assert world.world_for_name("Other1") == "Other1"


def test_switched_off_living_world_leaves_mantella_alone(tmp_path):
    world = LivingWorld(str(tmp_path), enabled=False)
    assert world.resolve_world("ZacharyLex1", {KEY_WORLD_KEY: "Adventurer1"}) == "ZacharyLex1"


def test_reply_always_names_the_world(tmp_path):
    reply, _ = talk(tmp_path, "Prisoner", "Prisoner1", {}, 2.0)   # older scripts ignore the extra key
    assert reply[KEY_WORLD_KEY] == "Prisoner1"
    assert worlds.world_exists(str(tmp_path), "Prisoner1")


def test_diary_world_ids_keep_the_dash_of_a_world_key():
    from livingworld import diary
    assert diary.clean_world_id("Adventurer1-2") == "Adventurer1-2"
    assert diary.clean_world_id("Zachary Lex1") == "ZacharyLex1"
