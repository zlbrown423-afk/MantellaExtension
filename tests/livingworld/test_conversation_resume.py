"""A conversation picked back up after a save load (livingworld/continuity/resume.py).

The game's OnPlayerLoadGame ends the conversation the save held (sending the loaded save's game time), then
starts it again with the same people and the conversation id the save carried. These tests play that sequence
against the real Mantella route:

    day 3.50  Torvar starts talking to Gerdur (timeline 1)
    day 3.51  "How old are you?"              -> Gerdur: "I'm 31."             (before the save)
    ---- the game is saved at day 3.53 ----
    day 3.56  "Here is a secret about Whiterun." (after the save)
    ---- the save from day 3.53 is loaded ----
"""
import sqlite3

import pytest

from livingworld.continuity import resume
from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from livingworld.memory import jobs
from src.http.communication_constants import communication_constants as comm_consts

SAVE_DAY = 3.53


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.delenv(resume.ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


def gerdur_llm(summary="Torvar and Gerdur talked."):
    llm = ScriptedLLM(
        replies=["Welcome to Riverwood."],
        rules=[when_player_says("how old", "I'm 31, since you ask."),
               when_player_says("secret", "A secret? I'll keep it, Torvar.")],
        extractions={"Gerdur": ScriptedExtraction(
            facts=[Fact("I'm 31", "Gerdur", "age", "31", "Gerdur"),
                   Fact("secret about Whiterun", "Torvar", "secret", "Whiterun", "Torvar")])})
    llm.summary_reply = summary
    return llm


def end_by_load(s, day=SAVE_DAY, flag=True):
    """What the game's OnPlayerLoadGame sends: an end request with the loaded save's game time."""
    request = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION,
               comm_consts.KEY_ENDCONVERSATION_TIMESTAMP: day}
    if flag:
        request[resume.KEY_LOAD_END] = True
    return s.post(request)


def talk_then_reload(s, after_save=True, people=None):
    """Plays the timeline above in session ``s`` up to the load; returns the first conversation's id."""
    people = people or [actors.player("Torvar"), actors.gerdur()]
    reply = s.start(people, game_days=3.5, extra=load(0, 101, 3.49))
    s.continue_until_player_turn()
    s.say("How old are you?", game_days=3.51)
    if after_save:
        s.say("Here is a secret about Whiterun.", game_days=3.56)
    end_by_load(s)
    return reply[resume.KEY_CONV_ID]


def restart(s, conv_id, people=None, nonce=102, day=SAVE_DAY):
    people = people or [actors.player("Torvar"), actors.gerdur()]
    extra = {**load(1, nonce, day)}
    if conv_id is not None:
        extra[resume.KEY_RESUME_CONV] = conv_id
    reply = s.start(people, game_days=day, extra=extra)
    s.continue_until_player_turn()
    return reply


# ---- the end time a load sends ------------------------------------------------------------------

def test_an_end_time_earlier_than_the_conversation_is_a_load():
    assert resume.end_time(3.53, 3.56, False) == (3.56, True)
    assert resume.end_time(3.53, 3.56, True) == (3.56, True)
    assert resume.end_time(3.60, 3.56, False) == (3.60, False)    # the ordinary end: later than the last line
    assert resume.end_time(3.5601, 3.5602, False) == (3.5601, False)   # within a game minute: not a load
    assert resume.end_time(3.53, 3.53, True) == (3.53, True)
    assert resume.end_time(None, 3.56, True) == (None, True)
    assert resume.end_time(3.53, None, False) == (3.53, False)


def test_the_conversation_ended_by_a_load_keeps_its_own_time(tmp_path):
    with HeadlessSession(str(tmp_path), gerdur_llm()) as s:
        conv = talk_then_reload(s)
    row = open_db(tmp_path).execute("SELECT started_gt, ended_gt FROM conversation WHERE conv_id = ?", (conv,)).fetchone()
    assert row["ended_gt"] == pytest.approx(3.56)   # not 3.53, the loaded save's time


def test_without_the_flag_a_backwards_end_time_is_still_caught(tmp_path):
    """A game build without the resume patch sends no flag; the time going backwards is enough."""
    with HeadlessSession(str(tmp_path), gerdur_llm()) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=3.5, extra=load(0, 101, 3.49))
        s.continue_until_player_turn()
        s.say("Here is a secret about Whiterun.", game_days=3.56)
        end_by_load(s, flag=False)
    conv = reply[resume.KEY_CONV_ID]
    assert open_db(tmp_path).execute("SELECT ended_gt FROM conversation WHERE conv_id = ?",
                                     (conv,)).fetchone()[0] == pytest.approx(3.56)


def test_mantella_summary_of_talk_after_the_save_is_not_seen_after_the_load(tmp_path):
    """Before: the summary was stamped with the loaded save's time and the loaded game saw it."""
    llm = gerdur_llm(summary="Torvar told Gerdur a secret about Whiterun.")
    with HeadlessSession(str(tmp_path), llm) as s:
        talk_then_reload(s)
        LivingWorld.latest.wait_for_jobs()
        llm.calls.clear()
        restart(s, None)
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert "secret about Whiterun" not in prompt


def test_the_start_reply_carries_the_conversation_id(tmp_path):
    with HeadlessSession(str(tmp_path), gerdur_llm()) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=3.5)
        s.continue_until_player_turn()
        s.end(game_days=3.6)
    assert reply[resume.KEY_CONV_ID] == 1
    assert reply[resume.KEY_RESUME_OFF] is False


# ---- picking it back up ------------------------------------------------------------------------

def test_the_npc_carries_on_with_what_was_said_before_the_save(tmp_path):
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s)
        llm.calls.clear()
        reply = restart(s, conv)
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert reply[KEY_TIMELINE_ID] == 2                       # the load forked a new timeline at day 3.53
    assert "I was in the middle of talking with Torvar" in prompt
    assert "do not greet Torvar again" in prompt
    assert "How old are you?" in prompt and "I'm 31" in prompt
    assert "secret" not in prompt                            # said after the save: never happened here
    assert "it ended like this" not in prompt                # the ordinary recap is not shown as well


def test_lines_before_the_save_are_carried_over_and_remembered_once(tmp_path):
    with HeadlessSession(str(tmp_path), gerdur_llm()) as s:
        conv = talk_then_reload(s)
        new = restart(s, conv)[resume.KEY_CONV_ID]
        s.say("Good to know.", game_days=3.57)
        s.end(game_days=3.58)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    link = db.execute("SELECT resumed_conv_id, lines_copied FROM conversation_resume WHERE conv_id = ?", (new,)).fetchone()
    assert tuple(link) == (conv, 3)   # greeting, "How old are you?", "I'm 31": what the save had
    texts = [r[0] for r in db.execute("SELECT text FROM utterance WHERE conv_id = ? ORDER BY seq", (new,))]
    assert texts[1:3] == ["How old are you?", "I'm 31, since you ask."]
    assert not any("secret" in t.lower() for t in texts)
    # the old conversation is untouched: it still holds everything, on the timeline the player left
    assert db.execute("SELECT COUNT(*) FROM utterance WHERE conv_id = ? AND text LIKE '%secret%'", (conv,)).fetchone()[0] == 2
    # what Gerdur knows in the loaded game: her age once, and nothing of the secret
    facts = [(r["predicate"], r["value"]) for r in db.execute("SELECT predicate, value FROM v_known_facts")]
    assert facts.count(("age", "31")) == 1
    assert ("secret", "Whiterun") not in facts
    # both conversations were extracted, each once: the old one on timeline 1, the new one on timeline 2
    done = [tuple(r) for r in db.execute("SELECT kind, status FROM job ORDER BY job_id")]
    assert done.count(("extract_conversation", "done")) == 2


def test_nothing_is_copied_when_nothing_was_said_after_the_save(tmp_path):
    """Saved, then loaded straight away: the old conversation's memories are visible, so copying its lines
    would extract them twice. They are shown, not copied."""
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s, after_save=False)
        llm.calls.clear()
        reply = restart(s, conv)
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()
    new = reply[resume.KEY_CONV_ID]
    db = open_db(tmp_path)
    assert reply[KEY_TIMELINE_ID] == 1                       # nothing after the save, so no fork
    assert db.execute("SELECT lines_copied FROM conversation_resume WHERE conv_id = ?", (new,)).fetchone()[0] == 0
    assert "I was in the middle of talking with Torvar" in prompt and "I'm 31" in prompt
    facts = [(r["predicate"], r["value"]) for r in db.execute("SELECT predicate, value FROM v_known_facts")]
    assert facts.count(("age", "31")) == 1


def test_someone_who_was_not_there_does_not_hear_the_copied_lines(tmp_path):
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s)
        llm.calls.clear()
        new = restart(s, conv, people=[actors.player("Torvar"), actors.gerdur(), actors.hod()])[resume.KEY_CONV_ID]
        s.end(game_days=3.6)
    db = open_db(tmp_path)
    joined = dict(db.execute("SELECT n.display_name, p.joined_seq FROM participant p JOIN npc n USING (npc_id)"
                             " WHERE p.conv_id = ?", (new,)).fetchall())
    assert joined["Gerdur"] == 0 and joined["Torvar"] == 0
    assert joined["Hod"] == 3          # after the three copied lines
    hod_prompt = next(c.system_prompt for c in llm.dialogue_calls)
    # group prompt: Gerdur's block has the talk in progress; Hod has no "in the middle" block of his own
    assert hod_prompt.count("I was in the middle of talking with Torvar") == 1


# ---- refusals: anything that does not clearly belong starts fresh -------------------------------------

@pytest.mark.parametrize("bogus", [9999, -4, "junk"])
def test_an_unknown_conversation_id_starts_fresh(tmp_path, bogus):
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        talk_then_reload(s)
        llm.calls.clear()
        new = restart(s, bogus)[resume.KEY_CONV_ID]
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert "I was in the middle" not in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM conversation_resume WHERE conv_id = ?", (new,)).fetchone()[0] == 0


def test_a_conversation_with_other_people_is_not_picked_up(tmp_path):
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s)
        llm.calls.clear()
        restart(s, conv, people=[actors.player("Torvar"), actors.hod()])
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert "I was in the middle" not in prompt


def test_a_conversation_from_long_before_is_not_picked_up(tmp_path):
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s, after_save=False)
        llm.calls.clear()
        restart(s, conv, day=5.0)
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=5.1)
    assert "I was in the middle" not in prompt


def test_a_conversation_the_loaded_save_never_reached_is_not_picked_up(tmp_path):
    """The id belongs to a conversation that started after the loaded save (a stale id from elsewhere)."""
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s)
        llm.calls.clear()
        restart(s, conv, day=3.45)      # a save from before the conversation started
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.47)
    assert "I was in the middle" not in prompt
    assert "How old" not in prompt


def test_switched_off_the_game_is_told_and_nothing_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.setenv(resume.ENV, "0")
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        conv = talk_then_reload(s)
        llm.calls.clear()
        reply = restart(s, conv)
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert reply[resume.KEY_RESUME_OFF] is True
    assert "I was in the middle" not in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM conversation_resume").fetchone()[0] == 0


# ---- the ordinary recap and background jobs respect the loaded save ------------------------------------------

def test_the_recap_leaves_out_lines_said_after_the_loaded_save(tmp_path):
    """Before: the recap of the last conversation showed every line, including those after the save."""
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        talk_then_reload(s)
        llm.calls.clear()
        restart(s, None)                 # talked to again by hand, no resume
        prompt = llm.dialogue_calls[0].system_prompt
        s.end(game_days=3.6)
    assert "it ended like this" in prompt and "I'm 31" in prompt
    assert "secret" not in prompt


def test_gossip_from_the_abandoned_talk_stays_on_its_own_timeline(tmp_path):
    """The extraction job can run after the load has forked a new timeline; its gossip claims belong to the
    conversation's timeline, not the new one (before: they were written to the active timeline)."""
    llm = gerdur_llm()
    llm.extractions["Gerdur"].reports = [{"about": "Hod", "did": "cheated at cards", "valence": -0.5,
                                          "phrase": "secret about Whiterun"}]
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], game_days=3.5, extra=load(0, 101, 3.49))
        s.continue_until_player_turn()
        s.say("Here is a secret about Whiterun: Hod cheated at cards.", game_days=3.56)
        end_by_load(s)
        restart(s, None)                 # the load forks timeline 2 before the job runs
        LivingWorld.latest.wait_for_jobs()
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    rows = db.execute("SELECT timeline_id FROM claim WHERE act = 'reported'").fetchall()
    assert rows and {r[0] for r in rows} == {1}
    assert db.execute("SELECT COUNT(*) FROM v_claim_knowledge WHERE act = 'reported'").fetchone()[0] == 0
