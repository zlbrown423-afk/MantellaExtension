"""Milestone 1, PR D (server side): loading an older save rolls NPC memory back to that save.

Follows the test plan's "Reload an older save" case: after Phase D, load M1_after_A and talk to
Hod (he knows nothing Gerdur shared), then load M1_after_D (he knows it again). Nothing is deleted.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID,
                                     LivingWorld)
from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


def play_to_phase_d(work_dir):
    """Phase A (Gerdur, day 3.5) then Phase D (Gerdur tells Hod, day 11), all on one timeline."""
    llm = ScriptedLLM(
        replies=["Welcome."], rules=[when_player_says("how old", "I'm 31.")],
        extractions={"Gerdur": ScriptedExtraction(facts=[Fact("I'm 31", "Gerdur", "age", "31", "Gerdur")])})
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=3.5, extra=load(0, 101, 3.4))
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()
    assert reply[KEY_TIMELINE_ID] == 1

    llm = ScriptedLLM(
        replies=["Hod, I'm 31, you know.", "Aye, I know."],
        extractions={"Hod": ScriptedExtraction(facts=[Fact("I'm 31", "Gerdur", "age", "31", "Gerdur")])})
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], game_days=11.0, extra=load(1, 102, 10.9))
        s.continue_until_player_turn()
        s.end(game_days=11.1)
        LivingWorld.latest.wait_for_jobs()


def hod_prompt(work_dir, timeline, nonce, loaded_day, day):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.hod()], game_days=day, extra=load(timeline, nonce, loaded_day))
        s.continue_until_player_turn()
        s.end(game_days=day + 0.05)
    return llm.dialogue_calls[0].system_prompt, reply[KEY_TIMELINE_ID]


def test_loading_an_older_save_forgets_what_came_after(tmp_path):
    play_to_phase_d(tmp_path)
    prompt, timeline = hod_prompt(tmp_path, timeline=1, nonce=201, loaded_day=4.0, day=4.1)  # M1_after_A
    assert timeline == 2
    assert "Gerdur's age is 31" not in prompt

    db = open_db(tmp_path)
    hod = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Hod'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE holder_id = ?", (hod,)).fetchone()[0] == 1  # still stored
    assert db.execute("SELECT COUNT(*) FROM v_known_facts WHERE holder_id = ?", (hod,)).fetchone()[0] == 0
    rows = [tuple(r) for r in db.execute("SELECT timeline_id, parent_id, branch_game_time, is_active FROM timeline")]
    assert rows == [(1, None, None, 0), (2, 1, 4.0, 1)]
    # Gerdur's own Phase A facts are from before the save, so they stay
    gerdur = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM v_known_facts WHERE holder_id = ?", (gerdur,)).fetchone()[0] == 1


def test_loading_the_later_save_again_brings_it_back(tmp_path):
    play_to_phase_d(tmp_path)
    hod_prompt(tmp_path, timeline=1, nonce=201, loaded_day=4.0, day=4.1)             # back to M1_after_A
    prompt, timeline = hod_prompt(tmp_path, timeline=1, nonce=202, loaded_day=11.2, day=11.3)  # M1_after_D
    assert timeline == 1
    assert "Gerdur told me that Gerdur's age is 31" in prompt
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 2           # no new branch
    assert db.execute("SELECT COUNT(*) FROM timeline WHERE is_active = 1").fetchone()[0] == 1


def test_talk_on_a_branch_is_forgotten_when_the_old_save_loads_again(tmp_path):
    play_to_phase_d(tmp_path)
    hod_prompt(tmp_path, timeline=1, nonce=201, loaded_day=4.0, day=4.1)   # branch 2: a talk with Hod at day 4.1
    _, timeline = hod_prompt(tmp_path, timeline=1, nonce=203, loaded_day=4.0, day=4.1)  # same save again
    assert timeline == 3
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT timeline_id, parent_id, is_active FROM timeline")] == \
        [(1, None, 0), (2, 1, 0), (3, 1, 1)]


def test_a_load_reported_twice_is_handled_once(tmp_path):
    play_to_phase_d(tmp_path)
    _, first = hod_prompt(tmp_path, timeline=1, nonce=201, loaded_day=4.0, day=4.1)
    _, again = hod_prompt(tmp_path, timeline=1, nonce=201, loaded_day=4.0, day=4.2)
    assert first == again == 2
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 2


def test_a_save_from_before_the_living_world_keeps_the_current_timeline(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    result = resolve_load(store, nonce=1, save_timeline_id=None, loaded_gt=50.0)
    assert (result.timeline_id, result.branched) == (1, False)
    assert resolve_load(store, nonce=2, save_timeline_id=99, loaded_gt=50.0).timeline_id == 1  # unknown id


def test_without_load_keys_nothing_changes(tmp_path):
    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Hello."])) as s:
        reply = s.start([actors.player(), actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.end(game_days=2.1)
    assert KEY_TIMELINE_ID not in reply
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM game_load").fetchone()[0] == 0
