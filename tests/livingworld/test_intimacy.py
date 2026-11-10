"""OStim scenes: NPCs know a scene is happening and what happens in it, and remember it as a scene.

The play session of 2026-11-10 had about a hundred grabs on Gerdur during OStim scenes, every one scored as
an unwanted grab (her decency and gentleness views of the player fell to -16 and -21). The game now folds
the player's hands and slaps on a partner into the scene and sends one ``scene`` record when it ends; these
tests drive that record through the real code path.
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, KEY_EVENTS, attire, grab, hit, scene, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.intimacy import scenes
from livingworld.intimacy.scenes import KEY_SCENE, act_words
from livingworld.perception.records import BadRecord, parse_record

PLAYER = actors.player("Torvar")
KISS_SEX_SPANK = ("kissing", "frenchkissing", "vaginalsex", "spanking", "holdinghand")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, npcs, day, events=None, live=None, snapshot=None):
    llm = ScriptedLLM(replies=["Hm."])
    context = {KEY_EVENTS: events or []}
    if live is not None:
        context[KEY_SCENE] = live
    if snapshot is not None:
        context[KEY_ATTIRE] = snapshot
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([PLAYER, *npcs], game_days=day, context_extra=context)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt, reply


def memories(db, name):
    return db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception' ORDER BY m.mem_id", (name,)).fetchall()


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def deltas(db, holder, other):
    rows = db.execute("SELECT deltas_json FROM relationship_change WHERE from_id = ? AND to_id = ?",
                      (npc_id(db, holder), npc_id(db, other))).fetchall()
    total: dict[str, float] = {}
    for (d,) in rows:
        for k, v in json.loads(d).items():
            total[k] = total.get(k, 0.0) + v
    return total


# ---- records ---------------------------------------------------------------------------------------

def test_scene_record_parses_and_keeps_only_plain_action_ids():
    rec = parse_record(scene([PLAYER, actors.gerdur()], 3.5, acts=("Kissing", "vaginalsex", "kissing", "bad id!"),
                             touches=12, slaps=3, climax=1))
    assert rec.type == "scene" and rec.via == "end" and rec.thread == 0
    assert rec.acts == ["kissing", "vaginalsex"]
    assert (rec.touches, rec.slaps, rec.climax, rec.forced) == (12, 3, 1, False)
    assert act_words(["frenchkissing", "vaginalsex", "spanking", "holdinghand"]) == ["kissing", "sex", "spanking"]
    with pytest.raises(BadRecord):
        parse_record(scene([PLAYER, actors.gerdur()], 3.5).replace("via=end", "via=sometime"))


# ---- a finished scene -----------------------------------------------------------------------------

def test_a_scene_is_remembered_as_shared_and_the_slaps_as_part_of_it(tmp_path):
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=KISS_SEX_SPANK, seconds=420, touches=40, slaps=5,
                location="Arcadia's Cauldron")
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    mem = memories(db, "Gerdur")[-1]
    assert mem["content"].startswith("Torvar and I were intimate in Arcadia's Cauldron for about 7 minutes:"
                                     " kissing, sex and spanking.")
    assert "part of it, not violence" in mem["interpretation"]
    assert mem["valence"] > 0
    d = deltas(db, "Gerdur", "Torvar")
    assert d.get("intimacy", 0) > 0 and "trust" not in d and "attraction" not in d
    # nothing about it reads as a grab, a hit or an assault
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type IN ('physical_interaction', 'violence')").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act IN ('grabbed', 'held', 'touched', 'hit')").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM standing_change WHERE holder_id = ? AND domain IN ('decency', 'gentleness')"
                      " AND delta < 0", (npc_id(db, "Gerdur"),)).fetchone()[0] == 0


def test_her_own_scene_is_not_hers_to_gossip_but_hod_seeing_it_hurts(tmp_path):
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=("kissing", "vaginalsex"),
                witnesses=[seen(actors.hod(), 5.0, rank_to_actor=0, rank_to_target=4)])
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    assert memories(db, "Hod")[-1]["content"] == "I saw Torvar and Gerdur being intimate in Whiterun."
    claim = db.execute("SELECT * FROM claim WHERE act = 'caught_with'").fetchone()
    assert claim["object_id"] == npc_id(db, "Gerdur")
    shares = dict(db.execute("SELECT n.display_name, k.will_share FROM knowledge k JOIN npc n ON n.npc_id = k.holder_id"
                             " WHERE k.claim_id = ?", (claim["claim_id"],)).fetchall())
    assert shares["Gerdur"] == "no"


def test_a_forced_scene_is_never_remembered_as_wanted(tmp_path):
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=("vaginalsex",), forced=True, slaps=4,
                witnesses=[seen(actors.hod(), 6.0)])
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    mem = memories(db, "Gerdur")[-1]
    assert "forced themselves on me" in mem["content"] and mem["valence"] < -0.5
    assert deltas(db, "Gerdur", "Torvar")["trust"] < -0.3
    assert "force themselves on Gerdur" in memories(db, "Hod")[-1]["content"]
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'forced'").fetchone()[0] == 1
    assert db.execute("SELECT type FROM world_event WHERE game_key LIKE 'scene-%'").fetchone()[0] == "intimacy.forced"


def test_children_are_never_part_of_a_scene(tmp_path):
    frodnar = actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")   # a child in Mantella's list
    talk(tmp_path, [actors.gerdur()], 3.51, events=[scene([PLAYER, frodnar], 3.5, witnesses=[seen(actors.gerdur(), 2.0)])])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type LIKE 'romance.scene'").fetchone()[0] == 0
    assert memories(db, "Gerdur") == []


def test_scenes_are_off_with_the_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_INTIMACY", "0")
    _, reply = talk(tmp_path, [actors.gerdur()], 3.51, events=[scene([PLAYER, actors.gerdur()], 3.5)])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type LIKE 'romance.scene'").fetchone()[0] == 0


# ---- while it runs ---------------------------------------------------------------------------------

def test_mid_scene_the_npc_knows_what_is_happening_and_that_slaps_are_part_of_it(tmp_path):
    live = [scene([PLAYER, actors.gerdur()], 3.5, via="now", acts=("kissing", "spanking"))]
    snap = [attire(actors.gerdur(), "none", 3.5, witnesses=[]), attire(PLAYER, "none", 3.5)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.5, live=live, snapshot=snap)
    assert "Gerdur is in the middle of an intimate scene with Torvar right now (kissing and spanking)" in prompt
    assert "slaps and rough play between Gerdur and Torvar right now are part of this scene, not an attack" in prompt
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type LIKE 'romance.%'").fetchone()[0] == 0


def test_a_bystander_who_can_see_it_knows_and_one_who_cannot_does_not():
    gerdur, hod, alvor = 1, 2, 3
    live = scenes.SceneNow({10: "Torvar", gerdur: "Gerdur"}, ["kissing"], seen_by={hod})
    assert scenes.lines_for(hod, "Hod", [live], {}) == ["Hod can see Torvar and Gerdur being intimate close by (kissing)."]
    assert scenes.lines_for(alvor, "Alvor", [live], {}) == []


def test_clothes_coming_off_for_a_scene_make_no_naked_memories(tmp_path):
    rec = attire(actors.gerdur(), "none", 3.5, via="scene", witnesses=[seen(actors.hod(), 4.0)])
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    assert not [m for m in memories(db, "Hod") if "no clothes" in m["content"] or "naked" in m["content"]]
    assert not memories(db, "Gerdur")


def test_grabs_and_hits_outside_a_scene_are_unchanged(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.51, events=[
        grab(PLAYER, actors.gerdur(), "NPC L Forearm [LLar]", 6.0, 3.5),
        hit(PLAYER, actors.gerdur(), 3.505)])
    db = open_db(tmp_path)
    kinds = {r[0] for r in db.execute("SELECT type FROM world_event")}
    assert {"physical_interaction", "violence"} <= kinds


# ---- the player's size (The New Gentleman) ----------------------------------------------------------

def test_size_rule_five_inches_or_less_is_unimpressive(monkeypatch):
    from livingworld.intimacy import size
    monkeypatch.delenv("MANTELLA_LW_TNG_LENGTH_IN", raising=False)
    monkeypatch.delenv("MANTELLA_LW_TNG_GIRTH_IN", raising=False)
    assert size.from_scale(0.8).length_in == 4.8 and not size.from_scale(0.8).impressive    # XS
    assert size.from_scale(0.9).verdict() == "impressive"                                   # S: 5.4 in
    assert size.from_scale(1.4).verdict() == "impressive, and thick"                        # XL: 8.4 x 6.7
    monkeypatch.setenv("MANTELLA_LW_TNG_LENGTH_IN", "5")
    assert size.from_scale(1.0).verdict() == "unimpressive"                                 # exactly 5 is not
    monkeypatch.setenv("MANTELLA_LW_TNG_GIRTH_IN", "4")
    assert size.from_scale(1.0).verdict() == "unimpressive, and thin"
    assert size.from_scale(None) is None


def test_a_woman_in_the_scene_remembers_and_notices_his_size(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.4)       # she is known (and known to be a woman) from an earlier talk
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=("vaginalsex",), player_size=1.2)
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    mem = memories(db, "Gerdur")[-1]
    assert "Torvar was about 7.2 inches long and 5.7 inches around; I found him impressive, and thick." in mem["content"]
    live = [scene([PLAYER, actors.gerdur()], 3.6, via="now", acts=("vaginalsex",), player_size=0.8)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.6, live=live)
    assert "Gerdur has seen Torvar's manhood up close: about 4.8 inches long and 3.8 inches around." in prompt
    assert "finds it unimpressive, and thin" in prompt


def test_men_and_forced_scenes_get_no_size_line(tmp_path):
    talk(tmp_path, [actors.hod()], 3.4)
    talk(tmp_path, [actors.hod()], 3.51, events=[scene([PLAYER, actors.hod()], 3.5, player_size=1.2)])
    talk(tmp_path, [actors.gerdur()], 3.52)
    talk(tmp_path, [actors.gerdur()], 3.53, events=[scene([PLAYER, actors.gerdur()], 3.525, forced=True, player_size=1.2)])
    db = open_db(tmp_path)
    assert all("inches" not in m["content"] for m in memories(db, "Hod") + memories(db, "Gerdur"))


def test_outside_a_scene_she_remembers_his_size_and_may_bring_it_up(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.4)
    talk(tmp_path, [actors.gerdur()], 3.51, events=[scene([PLAYER, actors.gerdur()], 3.5, player_size=1.2)])
    prompt, _ = talk(tmp_path, [actors.gerdur()], 4.2)     # the next day, dressed, an ordinary conversation
    assert ("Gerdur has been intimate with Torvar and remembers his size: about 7.2 inches long and 5.7 inches"
            " around, which she found impressive, and thick.") in prompt
    hod_prompt, _ = talk(tmp_path, [actors.hod()], 4.3)
    assert "remembers his size" not in hod_prompt


def test_a_woman_who_sees_him_naked_notices_and_remembers(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.4)
    naked = attire(PLAYER, "none", 3.5, witnesses=[seen(actors.gerdur(), 3.0)]) + "|psize=0.8"
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.5, events=[naked], snapshot=[naked])
    assert "Torvar has no clothes or armor on, naked." in prompt
    assert "Gerdur can see Torvar's manhood: about 4.8 inches long and 3.8 inches around. Gerdur finds it unimpressive, and thin" in prompt
    db = open_db(tmp_path)
    assert "He was about 4.8 inches long and 3.8 inches around; I found him unimpressive, and thin." in memories(db, "Gerdur")[-1]["content"]
