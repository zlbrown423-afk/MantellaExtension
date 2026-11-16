"""Private notes in group talk (livingworld/dialogue/private_notes.py).

Notes on the player's line that belong to one NPC (their place memories, what they noticed, a scene they
can see, their own preferences, a superior's guard record) reach only that NPC's AI call and summary.
Shared notes (said aloud, seen by all) still reach everyone.
"""
import copy
import pickle
import sqlite3

import pytest

from livingworld.dialogue import private_notes as PN
from livingworld.dialogue.group import thread_for
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from src.characters_manager import Characters
from src.llm.message_thread import message_thread
from src.llm.messages import AssistantMessage, UserMessage


class Who:
    def __init__(self, name, ref_id, is_player=False):
        self.name, self.ref_id, self.is_player_character = name, ref_id, is_player


HOD_NOTE = "(Hod also remembers, about Bleak Falls Barrow: I saw draugr walking near the barrow.)"
SHARED = "Torvar now knows Hod's name."


def player_line(config, *events):
    m = UserMessage(config, "Have you been to Bleak Falls Barrow?", "Torvar")
    m.add_event(list(events))
    return m


# ---- the note itself ---------------------------------------------------------------------------

def test_a_private_note_is_a_string_that_keeps_its_owner_through_copies():
    note = PN.PrivateNote(HOD_NOTE, 1234)
    assert note == HOD_NOTE and isinstance(note, str) and note.owner == "1234"
    for twin in (copy.deepcopy(note), copy.copy(note), pickle.loads(pickle.dumps(note))):
        assert isinstance(twin, PN.PrivateNote) and twin.owner == "1234" and twin == HOD_NOTE


def test_only_the_owner_sees_a_private_note_and_everyone_sees_a_shared_one():
    note = PN.PrivateNote(HOD_NOTE, "hod")
    assert PN.visible_to(note, "hod")
    assert not PN.visible_to(note, "lucan")
    assert not PN.visible_to(note, None)                   # an unknown viewer sees no private note
    assert not PN.visible_to(PN.PrivateNote("x", None), "hod")
    assert PN.visible_to(SHARED, "lucan") and PN.visible_to(SHARED, None)


def test_reowned_maps_npc_ids_to_ref_ids_and_never_frees_an_unmapped_note():
    out = PN.reowned([PN.PrivateNote("boss", 7), SHARED, PN.PrivateNote("lost", 99)], {7: "caius-ref"})
    assert out[0].owner == "caius-ref" and out[1] == SHARED and not isinstance(out[1], PN.PrivateNote)
    assert isinstance(out[2], PN.PrivateNote) and not PN.visible_to(out[2], "99")


def test_view_for_hides_other_npcs_notes_without_changing_the_thread(default_config):
    m = player_line(default_config, PN.PrivateNote(HOD_NOTE, "hod"), SHARED)
    lucan = PN.view_for(m, "lucan").get_formatted_content()
    hod = PN.view_for(m, "hod").get_formatted_content()
    assert "draugr" not in lucan and SHARED in lucan and "Bleak Falls Barrow?" in lucan
    assert "draugr" in hod and SHARED in hod
    assert "draugr" in m.get_formatted_content()            # the original keeps everything
    plain = player_line(default_config, SHARED)
    assert PN.view_for(plain, "lucan") is plain              # nothing private: no copy


# ---- group talk: each speaker's thread ---------------------------------------------------------

def test_group_thread_shows_a_private_note_only_to_its_owner(default_config):
    thread = message_thread(default_config, "system")
    thread.add_message(player_line(default_config, PN.PrivateNote(HOD_NOTE, "hod"), SHARED))
    for who, sees in ((Who("Hod", "hod"), True), (Who("Lucan", "lucan"), False)):
        text = "\n".join(m["content"] for m in thread_for(who, "prompt", thread, default_config, []).get_openai_messages())
        assert ("draugr" in text) is sees, who.name
        assert SHARED in text


# ---- summaries: an NPC never remembers another NPC's private note ------------------------------

def test_summary_threads_hold_only_the_npcs_own_private_notes(default_config, default_rememberer,
                                                              example_skyrim_npc_character,
                                                              another_example_skyrim_npc_character):
    guard, lydia = example_skyrim_npc_character, another_example_skyrim_npc_character
    thread = message_thread(default_config, "system prompt")
    chars = Characters()
    chars.add_or_update_character(guard, len(thread))
    chars.add_or_update_character(lydia, len(thread))
    thread.add_message(player_line(default_config, PN.PrivateNote("Guard's secret post.", guard.ref_id),
                                   PN.PrivateNote("Lydia's secret oath.", lydia.ref_id), SHARED))
    thread.add_message(AssistantMessage(default_config))
    chars.remove_character(guard, len(thread))
    chars.remove_character(lydia, len(thread))

    threads = default_rememberer.get_threads_for_summarization(thread, chars)

    def text(name):
        return " ".join(m.get_formatted_content() for m in threads[name].messages.get_talk_only())
    assert "Guard's secret post." in text("Guard") and "Lydia's secret oath." not in text("Guard")
    assert "Lydia's secret oath." in text("Lydia") and "Guard's secret post." not in text("Lydia")
    assert SHARED in text("Guard") and SHARED in text("Lydia")
    # different notes, different threads: one summary each instead of one shared
    assert sorted(map(sorted, default_rememberer.group_shared_threads(threads))) == [["Guard"], ["Lydia"]]


def test_summaries_with_only_shared_notes_are_still_grouped(default_config, default_rememberer,
                                                             example_skyrim_npc_character,
                                                             another_example_skyrim_npc_character):
    guard, lydia = example_skyrim_npc_character, another_example_skyrim_npc_character
    thread = message_thread(default_config, "system prompt")
    chars = Characters()
    chars.add_or_update_character(guard, len(thread))
    chars.add_or_update_character(lydia, len(thread))
    thread.add_message(player_line(default_config, SHARED))
    thread.add_message(AssistantMessage(default_config))
    threads = default_rememberer.get_threads_for_summarization(thread, chars)
    assert [sorted(g) for g in default_rememberer.group_shared_threads(threads)] == [["Guard", "Lydia"]]


# ---- end to end: Hod's place memory in a group with Gerdur -------------------------------------

@pytest.fixture
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def put_memory(db_path, owner_name, content, gt, place=None, importance=0.05):
    db = sqlite3.connect(db_path)
    with db:
        owner = db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (owner_name,)).fetchone()[0]
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        db.execute(
            "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
            " importance, valence, intensity, created_by, source_kind, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (timeline, owner, "episodic", content, "[]", place, gt, importance, 0.0, 0.1, "perception",
             "experienced", 0.9))
    db.close()


def speaker_of(call):
    return "Gerdur" if "You are Gerdur" in call.system_prompt else "Hod"


def non_system_text(call):
    return "\n".join(str(m.get("content")) for m in call.messages if m.get("role") != "system")


def test_hods_place_memory_reaches_hod_but_not_gerdur(tmp_path, living_world_inline, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_MEMORY_BOOST", "0")    # plain recency recall: the old draugr memory stays out
    def group(llm, line, day):
        with HeadlessSession(str(tmp_path), llm) as s:
            s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=day)
            s.continue_until_player_turn()
            if line:
                s.say(line)
            s.end(game_days=day + 0.1)

    group(ScriptedLLM(replies=["Hello."]), None, 10.0)       # both NPCs are in the database now
    # many filler memories so recall does not already put the draugr one in Hod's prompt
    db_path = tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite"
    for i in range(40):
        put_memory(db_path, "Hod", f"Chopped wood near the mill, day {i}.", 9.0 + i * 0.01, importance=0.9)
    put_memory(db_path, "Hod", "I saw draugr walking near Bleak Falls Barrow at night.", 2.0, "Bleak Falls Barrow")

    llm = ScriptedLLM(replies=["Hello there.", "Well met."],
                      rules=[when_player_says("Bleak Falls", "Hod would know about that.")])   # Hod answers too
    group(llm, "Gerdur, Hod, has anyone been up to Bleak Falls Barrow?", 11.0)

    calls = [c for c in llm.dialogue_calls if "Bleak Falls Barrow?" in non_system_text(c)]
    by_speaker = {speaker_of(c): non_system_text(c) for c in calls}
    assert set(by_speaker) == {"Gerdur", "Hod"}
    assert "Hod also remembers, about Bleak Falls Barrow" in by_speaker["Hod"]   # the note was made, for Hod
    assert "draugr" in by_speaker["Hod"]                      # Hod still remembers his own night
    assert "draugr" not in by_speaker["Gerdur"]               # ...and Gerdur is never told of it
    assert "Hod also remembers" not in by_speaker["Gerdur"]
    gerdur_prompts = [c.system_prompt for c in calls if speaker_of(c) == "Gerdur"]
    assert all("draugr" not in p for p in gerdur_prompts)


# ---- the superior's guard record stays with the superior ---------------------------------------

def test_first_orders_in_a_group_reach_the_superior_not_a_bystander(tmp_path, living_world_inline, monkeypatch):
    from tests.livingworld.test_guard import DAY, GUARD_ENV, PLAYER, caius
    from tests.livingworld.test_guard_duty import ENLIST, PAY, REPORT
    monkeypatch.delenv(GUARD_ENV, raising=False)
    llm = ScriptedLLM(rules=[ENLIST, REPORT, PAY], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius(), actors.hod()], location="Whiterun", game_days=DAY)
        s.continue_until_player_turn()
        s.say("Caius, I want to join the guard.", game_days=DAY + 0.0005)
        s.say("Hod, I'm in the guard now. Caius, what are my orders?", game_days=DAY + 0.001)   # Hod answers first
        s.say("Caius, understood.", game_days=DAY + 0.0015)
        s.end(game_days=DAY + 0.002)
        LivingWorld.latest.wait_for_jobs()

    def who(call):
        return "Hod" if "You are Hod" in call.system_prompt else "Caius"
    asked = [c for c in llm.dialogue_calls if "what are my orders" in c.all_text]
    assert {who(c) for c in asked} >= {"Hod", "Caius"}
    assert not any("real first orders" in c.all_text for c in asked if who(c) == "Hod")
    assert any("real first orders" in c.all_text for c in asked if who(c) == "Caius")

