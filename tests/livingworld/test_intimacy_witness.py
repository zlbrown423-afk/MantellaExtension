"""Witnessing scenes between NPCs, climaxes and their mess, the player's arousal, and covered eyes.

From the play session of 2026-11-10: Hod only ever learned about Gerdur and the player from Gerdur's own
gossip; he never saw a scene, and scenes between NPCs were seen by nobody. These tests drive the game's
records (as Mantella-Spell writes them) through the real code path.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, KEY_EVENTS, arousal, attire, climax, eyes, scene, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.intimacy import climax as climaxes
from livingworld.intimacy import scenes
from livingworld.intimacy.arousal import KEY_AROUSAL_KEY
from livingworld.intimacy.scenes import KEY_SCENE
from livingworld.perception.records import BadRecord, parse_record

PLAYER = actors.player("Torvar")
ALVOR = actors.actor("Alvor", 0x00013482, 0x0001348F, 0, "Nord", "MaleNord")
SIGRID = actors.actor("Sigrid", 0x00013485, 0x00013492, 1, "Nord", "FemaleNord")


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


def make_spouses(db, a, b):
    for x, y in ((a, b), (b, a)):
        db.execute("INSERT INTO relationship (from_id, to_id, status, kinship, updated_gt) VALUES (?,?,?,?,?)"
                   " ON CONFLICT(from_id, to_id) DO UPDATE SET kinship = 'spouse', status = 'spouse'",
                   (npc_id(db, x), npc_id(db, y), "spouse", "spouse", 1.0))
    db.commit()


def rel_changes(db, holder, other):
    return db.execute("SELECT deltas_json FROM relationship_change WHERE from_id = ? AND to_id = ?",
                      (npc_id(db, holder), npc_id(db, other))).fetchall()


# ---- scenes between NPCs -------------------------------------------------------------------------

def test_a_scene_between_npcs_is_seen_by_an_adult_bystander(tmp_path):
    rec = scene([actors.gerdur(), ALVOR], 3.5, acts=("kissing", "vaginalsex"),
                witnesses=[seen(SIGRID, 6.0)])
    talk(tmp_path, [SIGRID], 3.51, events=[rec])
    db = open_db(tmp_path)
    assert memories(db, "Sigrid")[-1]["content"] == "I saw Gerdur and Alvor being intimate in Whiterun."
    assert "Gerdur and I were intimate" in memories(db, "Alvor")[-1]["content"]


def test_hod_seeing_his_wife_with_someone_else_is_a_betrayal(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.4)
    make_spouses(open_db(tmp_path), "Gerdur", "Hod")
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=("vaginalsex",), witnesses=[seen(actors.hod(), 5.0)])
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    mem = memories(db, "Hod")[-1]
    assert mem["content"] == "I saw Gerdur being intimate with Torvar in Whiterun."
    assert mem["interpretation"].startswith("Gerdur is mine") and mem["valence"] < -0.5
    assert rel_changes(db, "Hod", "Gerdur") and rel_changes(db, "Hod", "Torvar")


def test_live_a_partner_sees_their_own_spouse_in_a_scene():
    hod, gerdur = 2, 1
    live = scenes.SceneNow({10: "Torvar", gerdur: "Gerdur"}, ["kissing"], seen_by={hod})
    line = scenes.lines_for(hod, "Hod", [live], {}, partners={gerdur})[0]
    assert line.startswith("Hod can see Gerdur, Hod's own partner, being intimate with Torvar close by")


def test_children_never_witness(tmp_path):
    frodnar = actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")
    rec = scene([actors.gerdur(), ALVOR], 3.5, witnesses=[seen(frodnar, 2.0)])
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    assert memories(db, "Frodnar") == []


# ---- climaxes and the mess ---------------------------------------------------------------------------

def test_where_a_climax_lands_follows_the_scene():
    assert climaxes.where(["blowjob", "kissing"]) == "face"
    assert climaxes.where(["boobjob"]) == "chest"
    assert climaxes.where(["vaginalsex", "gropingbreast"]) == "chest"
    assert climaxes.where(["vaginalsex"]) == "inside"
    assert climaxes.where(["handjob"]) == "body"


def test_a_climax_is_remembered_and_seen(tmp_path):
    rec = climax(PLAYER, actors.gerdur(), 3.5, acts=("blowjob",), witnesses=[seen(SIGRID, 4.0)])
    parse_record(rec)
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    db = open_db(tmp_path)
    assert memories(db, "Gerdur")[-1]["content"] == "Torvar finished on my face in Whiterun."
    assert memories(db, "Sigrid")[-1]["content"] == "I saw Torvar finish on Gerdur's face in Whiterun."


def test_the_mess_shows_until_a_bath(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.51, events=[climax(PLAYER, actors.gerdur(), 3.5, acts=("blowjob",))])
    snap = [attire(PLAYER, "clothing", 3.52), attire(actors.gerdur(), "clothing", 3.52)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.52, snapshot=snap)
    assert "Gerdur still has Torvar's semen on them from earlier (on their face)" in prompt
    prompt, _ = talk(tmp_path, [SIGRID, actors.gerdur()], 3.521, snapshot=snap + [attire(SIGRID, "clothing", 3.521)])
    assert "Sigrid can see that Gerdur has semen on their face." in prompt
    db = open_db(tmp_path)
    db.execute("INSERT INTO hygiene_change (timeline_id, npc_id, game_time, cause) VALUES"
               " ((SELECT timeline_id FROM timeline WHERE is_active = 1), ?, 3.53, 'bath')", (npc_id(db, "Gerdur"),))
    db.commit()
    prompt, _ = talk(tmp_path, [SIGRID, actors.gerdur()], 3.54, snapshot=snap)
    assert "semen" not in prompt


def test_inside_only_shows_on_someone_with_nothing_on(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.51, events=[climax(PLAYER, actors.gerdur(), 3.5, acts=("vaginalsex",))])
    dressed = [attire(PLAYER, "clothing", 3.52), attire(actors.gerdur(), "clothing", 3.52)]
    prompt, _ = talk(tmp_path, [SIGRID, actors.gerdur()], 3.52, snapshot=dressed)
    assert "running down" not in prompt
    bare = [attire(PLAYER, "clothing", 3.53), attire(actors.gerdur(), "none", 3.53)]
    prompt, _ = talk(tmp_path, [SIGRID, actors.gerdur()], 3.53, snapshot=bare)
    assert "Sigrid can see that semen is running down Gerdur's thighs." in prompt


# ---- arousal ---------------------------------------------------------------------------------------

def test_a_naked_aroused_player_is_noticed_and_women_react_their_own_way(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.4)
    snap = [attire(PLAYER, "none", 3.5, erect=True, player_size=1.2), attire(actors.gerdur(), "clothing", 3.5)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.5, snapshot=snap)
    assert "Gerdur can see that Torvar is naked and visibly aroused." in prompt
    assert "7.2 inches" in prompt   # her take on his size (perception/awareness.py)
    assert "How Gerdur reacts is her own" in prompt
    assert "does not make Gerdur any more willing" in prompt


def test_arousal_under_clothes_is_not_seen(tmp_path):
    snap = [attire(PLAYER, "clothing", 3.5, erect=True), attire(actors.gerdur(), "clothing", 3.5)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.5, snapshot=snap)
    assert "aroused" not in prompt
    talk(tmp_path, [actors.gerdur()], 3.51, events=[arousal(PLAYER, 3.5, cover="clothing", witnesses=[seen(actors.hod(), 2.0)])])
    assert memories(open_db(tmp_path), "Hod") == []


def test_witnesses_remember_a_naked_aroused_player(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.51, events=[arousal(PLAYER, 3.5, witnesses=[seen(actors.hod(), 3.0)])])
    assert memories(open_db(tmp_path), "Hod")[-1]["content"] == "I saw Torvar naked and visibly aroused in Riverwood."


def test_the_arousal_key_goes_out_with_the_start_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_AROUSAL_KEY", "72")
    _, reply = talk(tmp_path, [actors.gerdur()], 3.5)
    assert reply.get(KEY_AROUSAL_KEY) == 72


# ---- covered eyes ----------------------------------------------------------------------------------

def test_npcs_know_when_the_player_has_covered_their_eyes(tmp_path):
    snap = [attire(PLAYER, "clothing", 3.5, eyes_covered=True), attire(actors.gerdur(), "clothing", 3.5)]
    prompt, _ = talk(tmp_path, [actors.gerdur()], 3.5, snapshot=snap)
    assert "Torvar has their eyes covered right now and cannot see anything, but can still hear" in prompt
    rec = eyes(PLAYER, 3.5, witnesses=[seen(actors.hod(), 3.0)])
    assert parse_record(rec).via == "covered"
    with pytest.raises(BadRecord):
        parse_record(rec.replace("via=covered", "via=peeking"))
