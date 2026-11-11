"""Scenes seen along the way, and scenes still running when the conversation closes.

From the play session of 2026-11-11 (Bits and Pieces): the live snapshots told Sayma and Gerdur they could see
Gisli's scene, but the game's end record, sampled at the end, had Gerdur with no view and no Sayma, so only
Gisli remembered it. And the second scene with Gisli ran on after the last conversation; its end record waited
in the game's queue for a request that never came, so it was never stored at all.
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, climax, scene, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.intimacy.scenes import KEY_SCENE

PLAYER = actors.player("Torvar")
ALVOR = actors.actor("Alvor", 0x00013482, 0x0001348F, 0, "Nord", "MaleNord")
SIGRID = actors.actor("Sigrid", 0x00013485, 0x00013492, 1, "Nord", "FemaleNord")
HILDE = actors.actor("Hilde", 0x00013486, 0x00013493, 1, "Nord", "FemaleNord")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def memories(db, name):
    return db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception' ORDER BY m.mem_id", (name,)).fetchall()


def scene_events(db):
    return db.execute("SELECT * FROM world_event WHERE type IN ('romance.scene', 'intimacy.forced')"
                      " ORDER BY event_id").fetchall()


def witnesses(db, event_id):
    return {r[0]: r[1] for r in db.execute(
        "SELECT n.display_name, w.how FROM event_witness w JOIN npc n ON n.npc_id = w.npc_id WHERE w.event_id = ?",
        (event_id,))}


def conversation(work_dir, npcs, day, first=None, then=None):
    """One conversation: ``first`` is the start request's extra context, ``then`` a later request's (the player
    speaks), each like {KEY_EVENTS: [...], KEY_SCENE: [...]}."""
    llm = ScriptedLLM(replies=["Hm."] * 10)
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, context_extra=first or {})
        s.continue_until_player_turn()
        if then is not None:
            s.say("Well.", game_days=day + 0.002, context_extra=then)
        s.end(game_days=day + 0.003)


# ---- the snapshots' seers -------------------------------------------------------------------------

def test_someone_the_snapshots_showed_seeing_it_is_a_witness_of_the_end_record(tmp_path):
    live = [scene([ALVOR, actors.gerdur()], 3.5, via="now", acts=("vaginalsex",), thread=2,
                  witnesses=[seen(SIGRID, 4.0), seen(HILDE, 5.0)])]
    end = scene([ALVOR, actors.gerdur()], 3.51, acts=("vaginalsex",), thread=2,
                witnesses=[seen(HILDE, 9.0, los=False)])         # sampled at the end: Hilde had turned away, no Sigrid
    conversation(tmp_path, [HILDE], 3.5, first={KEY_SCENE: live}, then={KEY_EVENTS: [end], KEY_SCENE: []})
    db = open_db(tmp_path)
    events = scene_events(db)
    assert len(events) == 1
    assert witnesses(db, events[0]["event_id"]) == {"Alvor": "participated", "Gerdur": "participated",
                                                     "Sigrid": "saw", "Hilde": "saw"}
    assert memories(db, "Sigrid")[-1]["content"] == "I saw Alvor and Gerdur being intimate in Whiterun."
    assert memories(db, "Hilde")[-1]["content"] == "I saw Alvor and Gerdur being intimate in Whiterun."
    assert json.loads(events[0]["payload_json"])["nearby_but_unseen"] == []


def test_a_climax_also_gets_the_snapshots_seers(tmp_path):
    live = [scene([ALVOR, actors.gerdur()], 3.5, via="now", acts=("vaginalsex",), thread=2,
                  witnesses=[seen(SIGRID, 4.0)])]
    finish = climax(ALVOR, actors.gerdur(), 3.505, acts=("vaginalsex",), thread=2)
    conversation(tmp_path, [HILDE], 3.5, first={KEY_SCENE: live}, then={KEY_EVENTS: [finish], KEY_SCENE: live})
    db = open_db(tmp_path)
    assert any(m["content"] == "I saw Alvor finish inside Gerdur in Whiterun." for m in memories(db, "Sigrid"))


def test_a_different_scene_does_not_borrow_seers(tmp_path):
    live = [scene([ALVOR, actors.gerdur()], 3.5, via="now", thread=2, witnesses=[seen(SIGRID, 4.0)])]
    other = scene([PLAYER, HILDE], 3.51, thread=0)
    conversation(tmp_path, [HILDE], 3.5, first={KEY_SCENE: live}, then={KEY_EVENTS: [other], KEY_SCENE: []})
    db = open_db(tmp_path)
    hilde_scene = [e for e in scene_events(db) if json.loads(e["payload_json"])["thread"] == 0]
    assert len(hilde_scene) == 1
    assert "Sigrid" not in witnesses(db, hilde_scene[0]["event_id"])


# ---- a scene still running when the conversation closes -----------------------------------------

def test_a_scene_running_at_close_is_stored_once_with_its_seers(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing", "vaginalsex"),
                  witnesses=[seen(SIGRID, 4.0)])]
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_SCENE: live})
    db = open_db(tmp_path)
    events = scene_events(db)
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["ongoing"] is True
    assert witnesses(db, events[0]["event_id"]) == {"Torvar": "participated", "Gerdur": "participated",
                                                     "Sigrid": "saw"}
    assert memories(db, "Gerdur")[-1]["content"] == "Torvar and I were intimate in Whiterun: kissing and sex."
    assert memories(db, "Sigrid")[-1]["content"] == "I saw Torvar and Gerdur being intimate in Whiterun."


def test_the_late_end_record_completes_the_running_scene(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing",), witnesses=[seen(SIGRID, 4.0)])]
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_SCENE: live})
    end = scene([PLAYER, actors.gerdur()], 3.51, acts=("kissing", "vaginalsex", "spanking"), seconds=540.0,
                touches=6, slaps=3, climax=2, witnesses=[seen(SIGRID, 4.0), seen(HILDE, 3.0)], key="scene-end-77")
    conversation(tmp_path, [actors.gerdur()], 3.6, first={KEY_EVENTS: [end]})
    db = open_db(tmp_path)
    events = scene_events(db)
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["ongoing"] is False and payload["stored_while_running"] is True
    assert payload["acts"] == ["kissing", "vaginalsex", "spanking"]
    assert (payload["touches"], payload["slaps"], payload["climax"]) == (6, 3, 2)
    assert events[0]["duration_s"] == 540.0 and events[0]["game_key"] == "scene-end-77"
    assert len(memories(db, "Gerdur")) == 1
    assert len(memories(db, "Sigrid")) == 1
    assert memories(db, "Hilde")[-1]["content"] == "I saw Torvar and Gerdur being intimate in Whiterun."
    assert "Hilde" in witnesses(db, events[0]["event_id"])
    # the same end record sent again is a duplicate, not a second scene
    conversation(tmp_path, [actors.gerdur()], 3.7, first={KEY_EVENTS: [end]})
    assert len(scene_events(open_db(tmp_path))) == 1


def test_an_end_record_for_another_thread_is_its_own_scene(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing",), thread=0)]
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_SCENE: live})
    other = scene([PLAYER, actors.gerdur()], 3.51, acts=("vaginalsex",), thread=3)
    conversation(tmp_path, [actors.gerdur()], 3.6, first={KEY_EVENTS: [other]})
    db = open_db(tmp_path)
    events = scene_events(db)
    assert len(events) == 2
    assert [json.loads(e["payload_json"]).get("ongoing", False) for e in events] == [True, False]
    assert len(memories(db, "Gerdur")) == 2


def test_closing_twice_during_one_scene_keeps_one_event(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing",))]
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_SCENE: live})
    later = [scene([PLAYER, actors.gerdur()], 3.502, via="now", acts=("vaginalsex",), witnesses=[seen(SIGRID, 3.0)])]
    conversation(tmp_path, [actors.gerdur()], 3.502, first={KEY_SCENE: later})
    db = open_db(tmp_path)
    events = scene_events(db)
    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["acts"] == ["kissing", "vaginalsex"]
    assert len(memories(db, "Gerdur")) == 1
    assert len(memories(db, "Sigrid")) == 1


def test_a_snapshot_whose_scene_already_ended_is_not_stored_again(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing",))]
    end = scene([PLAYER, actors.gerdur()], 3.501, acts=("kissing",))
    # the end record arrives while the request's snapshot still lists the scene (stale snapshot)
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_SCENE: live}, then={KEY_EVENTS: [end]})
    assert len(scene_events(open_db(tmp_path))) == 1


def test_a_finished_scene_still_cannot_be_changed(tmp_path):
    conversation(tmp_path, [actors.gerdur()], 3.5, first={KEY_EVENTS: [scene([PLAYER, actors.gerdur()], 3.5)]})
    db = open_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE world_event SET payload_json = '{}' WHERE type = 'romance.scene'")
