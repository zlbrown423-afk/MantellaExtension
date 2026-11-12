"""Milestone 1, PR C: one NPC per AI call in group talk, the voice check, and attributed sharing.

These invert the upstream ``baseline_behaviour`` test: with the Living World on, no dialogue
prompt holds two NPCs' identities or memories (test plan isolation table and checkpoint D1).
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.dialogue.group import mentions

GERDUR_BIO = "Attractive adult woman with long blonde hair"
HOD_BIO = "Thick mustache"


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def npc_ids(db):
    return {r["display_name"]: r["npc_id"] for r in db.execute("SELECT npc_id, display_name FROM npc")}


def gerdur_meets_torvar(work_dir):
    llm = ScriptedLLM(
        replies=["Welcome to Riverwood."],
        rules=[when_player_says("how old", "I'm 31, not that it's any of your business."),
               when_player_says("spiders", "Spiders? I'll keep the broom handy.")],
        extractions={"Gerdur": ScriptedExtraction(
            facts=[Fact("I'm 31", "Gerdur", "age", "31", "Gerdur"),
                   Fact("Falkreath", "Torvar", "hometown", "Falkreath", "Torvar"),
                   Fact("spiders terrify", "Torvar", "fear", "spiders", "Torvar")],
            memories=["Torvar from Falkreath told me spiders terrify him."])},
    )
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.5)
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.say("I grew up in Falkreath, and spiders terrify me.")
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()


def group(work_dir, llm, *player_lines, day=11.0):
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=day)
        s.continue_until_player_turn()
        for line in player_lines:
            s.say(line)
        s.end(game_days=day + 0.1)
    return s


# ---- prompts: one identity each ---------------------------------------------------------------

def test_each_group_prompt_holds_one_npcs_identity_and_memories(tmp_path):
    gerdur_meets_torvar(tmp_path)
    llm = ScriptedLLM(replies=["Hod, this is Torvar.", "Well met, Torvar."])
    group(tmp_path, llm)

    prompts = {}
    for call in llm.dialogue_calls:
        speaker = "Gerdur" if "You are Gerdur" in call.system_prompt else "Hod"
        prompts.setdefault(speaker, call.system_prompt)
    assert set(prompts) == {"Gerdur", "Hod"}

    gerdur, hod = prompts["Gerdur"], prompts["Hod"]
    assert GERDUR_BIO in gerdur and HOD_BIO not in gerdur
    assert HOD_BIO in hod and GERDUR_BIO not in hod
    assert "Torvar told me that Torvar's fear is spiders" in gerdur
    for leaked in ("spiders", "Falkreath", "My age is 31", "What Gerdur knows"):
        assert leaked not in hod
    # each knows who else is present, as a name only
    assert "Hod is also here" in gerdur and "Gerdur is also here" in hod


def test_hod_sees_gerdurs_lines_as_hers(tmp_path):
    llm = ScriptedLLM(replies=["Hod, this is Torvar.", "Well met, Torvar."])
    group(tmp_path, llm)
    hod_call = next(c for c in llm.dialogue_calls if "You are Hod" in c.system_prompt)
    assert any(m["role"] == "user" and "Gerdur: Hod, this is Torvar." in m["content"] for m in hod_call.messages)
    assert not any(m["role"] == "assistant" and "this is Torvar" in m["content"] for m in hod_call.messages)


# ---- who speaks ---------------------------------------------------------------------------------

def test_the_npc_the_player_names_answers_first(tmp_path):
    llm = ScriptedLLM(replies=["Hello.", "Aye."],
                      rules=[when_player_says("Hod, how", "The mill keeps me busy.")])
    s = group(tmp_path, llm, "Hod, how is the mill?")
    after_question = s.lines[-1]
    assert (after_question.speaker, after_question.text) == ("Hod", "The mill keeps me busy.")


def test_a_named_npc_answers_and_turns_stop_at_two(tmp_path):
    llm = ScriptedLLM(replies=["Hod, say hello to our guest.", "Hello, Gerdur. Hello, guest.", "Never reached."])
    s = group(tmp_path, llm)
    assert [l.speaker for l in s.lines] == ["Gerdur", "Hod"]
    assert all(l.text != "Never reached." for l in s.lines)


# ---- text: no speaking for someone else -------------------------------------------------------

def test_a_reply_cannot_switch_to_another_speaker(tmp_path):
    llm = ScriptedLLM(replies=["Welcome. Hod: I'll speak for myself, thanks.", "Hello there."])
    s = group(tmp_path, llm)
    assert ("Gerdur", "Welcome.") in [(l.speaker, l.text) for l in s.lines]
    assert all("speak for myself" not in l.text for l in s.lines)
    voices = {line.text: line.voice for line in s.tts.lines}
    assert voices["Welcome."] == "FemaleNord"


def test_a_line_starting_with_a_name_stays_with_its_speaker(tmp_path):
    llm = ScriptedLLM(replies=["Hod, come meet our guest.", "Well met."])
    s = group(tmp_path, llm)
    assert (s.lines[0].speaker, s.lines[0].text) == ("Gerdur", "Hod, come meet our guest.")
    assert {line.text: line.voice for line in s.tts.lines}["Hod, come meet our guest."] == "FemaleNord"


# ---- database: one speaker per call (D1) and traces (C2) -------------------------------------

def test_every_dialogue_call_has_one_speaker(tmp_path):
    gerdur_meets_torvar(tmp_path)
    group(tmp_path, ScriptedLLM(replies=["Hod, this is Torvar.", "Well met.", "And the mill?", "Busy."]),
          "Gerdur, what about the mill?")
    db = open_db(tmp_path)
    ids = npc_ids(db)
    conv = db.execute("SELECT MAX(conv_id) FROM conversation").fetchone()[0]
    rows = db.execute("SELECT npc_id, trace_id, status FROM llm_request WHERE task = 'dialogue' AND conv_id = ?",
                      (conv,)).fetchall()
    assert rows and all(r["npc_id"] is not None and r["status"] == "ok" for r in rows)
    assert {r["npc_id"] for r in rows} == {ids["Gerdur"], ids["Hod"]}

    # C2: every knowledge or memory row a prompt held belongs to that prompt's speaker
    for r in rows:
        inputs = json.loads(db.execute("SELECT inputs_json FROM decision_trace WHERE trace_id = ?",
                                       (r["trace_id"],)).fetchone()[0])
        for know_id in inputs["know"]:
            assert db.execute("SELECT holder_id FROM knowledge WHERE know_id = ?", (know_id,)).fetchone()[0] == r["npc_id"]
        for mem_id in inputs["mems"]:
            assert db.execute("SELECT owner_id FROM memory WHERE mem_id = ?", (mem_id,)).fetchone()[0] == r["npc_id"]


def test_one_on_one_calls_are_recorded_too(tmp_path):
    gerdur_meets_torvar(tmp_path)
    db = open_db(tmp_path)
    rows = db.execute("SELECT npc_id FROM llm_request WHERE task = 'dialogue'").fetchall()
    assert len(rows) == 3 and {r["npc_id"] for r in rows} == {npc_ids(db)["Gerdur"]}


# ---- voice: a mismatch is refused before synthesis --------------------------------------------

def test_a_line_in_the_wrong_voice_is_not_synthesized(tmp_path):
    gerdur_meets_torvar(tmp_path)
    db = open_db(tmp_path)
    gerdur = npc_ids(db)["Gerdur"]
    with db:  # simulate Mantella choosing a different voice than the one bound
        db.execute("DELETE FROM voice_binding WHERE npc_id = ?", (gerdur,))
        db.execute("INSERT INTO voice_binding (npc_id, tts_service, voice_model, bound_gt) VALUES (?, 'piper', 'MaleOrc', 1)",
                   (gerdur,))

    llm = ScriptedLLM(replies=["Welcome back."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=5.0)
        s.continue_until_player_turn()
        s.end(game_days=5.1)
        assert LivingWorld.latest.errors >= 1
    assert all(line.text.strip() != "Welcome back." for line in s.tts.lines)
    assert all(line.text.strip() != "Welcome back." for line in s.lines)


def test_a_guard_rebuilt_with_another_game_voice_type_is_voiced(tmp_path):
    # Leveled guards can come back from a cell reset with another voice type on the same
    # reference. The line is voiced in the new voice and the binding follows the game.
    def guard(voice_type):
        return actors.actor("Whiterun Guard", 0x000EA0A2, 0x001000F7, 0, "Nord", voice_type)

    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Halt."])) as s:
        s.start([actors.player("Torvar"), guard("MaleNordCommander")], location="Whiterun", game_days=2.0)
        s.continue_until_player_turn()
        s.end(game_days=2.1)
    db = open_db(tmp_path)
    guard_id = npc_ids(db)["Whiterun Guard"]
    first = db.execute("SELECT voice_model, game_voice_type FROM voice_binding WHERE npc_id = ?", (guard_id,)).fetchone()
    assert first["game_voice_type"] == "MaleNordCommander"

    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["The Battle-Borns live north of here."])) as s:
        s.start([actors.player("Torvar"), guard("MaleGuard")], location="Whiterun", game_days=2.2)
        s.continue_until_player_turn()
        s.end(game_days=2.3)
        assert LivingWorld.latest.errors == 0
    assert any("Battle-Borns" in line.text for line in s.tts.lines)
    now = db.execute("SELECT voice_model, game_voice_type FROM voice_binding WHERE npc_id = ?", (guard_id,)).fetchone()
    assert now["game_voice_type"] == "MaleGuard"


def test_a_cached_guard_is_rebound_by_the_voice_check(tmp_path):
    # The voice check also follows the game when the NPC was registered earlier in this run.
    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Halt."])) as s:
        s.start([actors.player("Torvar"), actors.actor("Whiterun Guard", 0x000EA0A2, 0x001000F7, 0, "Nord",
                                                       "MaleGuard")], location="Whiterun", game_days=2.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        guard_id = next(i for i in lw._npc_ids.values() if lw.store.scalar(
            "SELECT display_name FROM npc WHERE npc_id = ?", (i,)) == "Whiterun Guard")
        with lw.store.transaction() as db:   # as if first bound while the reference had another voice type
            db.execute("DELETE FROM voice_binding WHERE npc_id = ?", (guard_id,))
            db.execute("INSERT INTO voice_binding (npc_id, tts_service, voice_model, game_voice_type, bound_gt)"
                       " VALUES (?, 'piper', 'Male Nord Commander', 'MaleNordCommander', 1)", (guard_id,))

        class Guard:
            ref_id = next(r for r, i in lw._npc_ids.items() if i == guard_id)
            name = "Whiterun Guard"
            is_player_character = False
            tts_service = "piper"
            tts_voice_model = "Male Guard"
            in_game_voice_model = "MaleGuard"

        assert lw.voice_allowed(Guard()) is True
        assert lw.store.scalar("SELECT voice_model FROM voice_binding WHERE npc_id = ?", (guard_id,)) == "Male Guard"
        Guard.tts_voice_model = "Male Nord"          # same game type, other model: still refused
        assert lw.voice_allowed(Guard()) is False
        s.end(game_days=2.1)


# ---- sharing: Gerdur tells Hod (test plan phases D and E) -------------------------------------

def test_gerdur_tells_hod_and_hod_repeats_it_with_attribution(tmp_path):
    gerdur_meets_torvar(tmp_path)
    llm = ScriptedLLM(
        replies=["Hod, this is Torvar.", "Well met."],
        rules=[when_player_says("tell hod", "Hod, I'm 31, and Torvar here grew up in Falkreath.")],
        extractions={"Gerdur": ScriptedExtraction(), "Hod": ScriptedExtraction(facts=[
            Fact("I'm 31", "Gerdur", "age", "31", "Gerdur"),
            Fact("grew up in Falkreath", "Torvar", "hometown", "Falkreath", "Gerdur")])},
    )
    group(tmp_path, llm, "Gerdur, tell Hod about yourself and where I'm from.")
    LivingWorld.latest.wait_for_jobs()

    hod_llm = ScriptedLLM(replies=["Gerdur told me she's 31."])
    with HeadlessSession(str(tmp_path), hod_llm) as s:
        s.start([actors.player("Torvar"), actors.hod()], location="Riverwood", game_days=12.0)
        s.continue_until_player_turn()
        s.end(game_days=12.1)
    prompt = hod_llm.dialogue_calls[0].system_prompt
    assert "Gerdur told me that Gerdur's age is 31" in prompt
    assert "Gerdur told me that Torvar's hometown is Falkreath" in prompt
    assert "spiders" not in prompt          # E3: never shared
    assert GERDUR_BIO not in prompt         # her identity stays hers

    db = open_db(tmp_path)
    ids = npc_ids(db)
    # E2: Hod has no self facts copied from Gerdur
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE holder_id = ? AND subject_id = ?",
                      (ids["Hod"], ids["Hod"])).fetchone()[0] == 0
    # E4: no memory row is shared between owners
    assert db.execute("SELECT COUNT(*) FROM (SELECT content FROM memory GROUP BY content"
                      " HAVING COUNT(DISTINCT owner_id) > 1)").fetchone()[0] == 0


def test_mentions_matches_whole_names_only():
    assert mentions("Hod, come here", "Hod") == 0
    assert mentions("Hodgepodge of things", "Hod") == -1
    assert mentions("Ask the whiterun guard", "Whiterun Guard") == 8
