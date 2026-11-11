"""Alfhild and the farm (in-game session 2026-11-10, Whiterun): she agreed to the player's help with the
harvest and said "Let's head to the farm... Follow me!", "I'll lead the way", "I'm right behind you",
but nobody walked anywhere and no task was recorded.

- "the farm" / "home" in a line now means the place named earlier in the talk, or in the NPC's bio
  (livingworld/dialogue/trips.py), so the walk question is asked;
- "Lead the way, I'm following you" answers that question with a yes;
- "Come on, follow me" + "I'm right behind you" asks "Have Alfhild follow you?" and a yes carries Follow;
- Whiterun has work (livingworld/work/data/whiterun_work.json), givers outside the social seeds are named
  by form id, and an offer of help the NPC takes up in words records the task (livingworld/work/words.py).
"""
import sqlite3

import pytest

from livingworld.dialogue import trips
from livingworld.dialogue.trips import FOLLOW, LEAD_TO, TripPlanner, TripWatcher, generic_words, places_in
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_HINTS, LivingWorld
from livingworld.memory import jobs
from livingworld.work import TASKS_ENV, words
from livingworld.work.templates import by_key, form_ref, templates
from src.actions.function_manager import FunctionManager
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Zachary")
ALFHILD = "Alfhild Battle-Born"
BIO = ("She is married to Idolaf Battle-Born. Alfhild's role in the family is to maintain the Battle-Born Farm. "
       "This farm grows leeks, wheat, and gourds, and she will be willing to pay the player to assist in the harvest.")


@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(trips.TRIPS_ENV, raising=False)
    monkeypatch.delenv(TASKS_ENV, raising=False)
    monkeypatch.delenv(words.WORDS_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


@pytest.fixture(autouse=True)
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


def play(planner, lines, offered=(LEAD_TO, FOLLOW), npc=ALFHILD):
    """Feed lines ('P' player / 'N' NPC). Returns [(line, actions added, notices)] for the NPC lines,
    and the player's notes."""
    out, notes = [], []
    for who, text in lines:
        if who == "P":
            notes.append(planner.player_said(text))
            continue
        acts: list = []
        TripWatcher(planner, [], set(offered)).keep(npc, text, acts)
        out.append((text, acts, planner.take_notices()))
    return out, notes


# ---- trips: the farm, follow me ----

def test_the_farm_means_the_farm_she_named_earlier():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, _ = play(p, [("N", "I run the Battle-Born Farm, where we grow leeks, wheat, and gourds."),
                      ("P", "Of course, let's do this."),
                      ("N", "Let's head to the farm and get started."),
                      ("N", "Follow me!")])
    assert out[1][2] == ["Go with Alfhild to Battle-Born Farm? Say yes or no."]
    assert out[2][2] == []                     # asked once


def test_the_farm_from_her_bio_when_nobody_named_it():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    p.learn_bio(ALFHILD, BIO)
    out, _ = play(p, [("P", "I'll help."), ("N", "Wonderful. Let's head to the farm.")])
    assert out[0][2] == ["Go with Alfhild to Battle-Born Farm? Say yes or no."]


def test_follow_me_alone_uses_the_place_named_just_before():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, _ = play(p, [("N", "I tend the fields at Battle-Born Farm."), ("P", "Sure."), ("N", "Follow me!")])
    assert out[1][2] == ["Go with Alfhild to Battle-Born Farm? Say yes or no."]


def test_lead_the_way_im_following_you_is_a_yes():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, notes = play(p, [("N", "I run the Battle-Born Farm."), ("P", "Okay."),
                          ("N", "Let's head to the farm. Follow me!"),
                          ("P", "Lead the way, I'm following you."),
                          ("N", "Come on, I'll lead the way!")])
    assert "Alfhild Battle-Born now leads Zachary to Battle-Born Farm" in notes[-1]
    assert out[-1][1] == [{"identifier": LEAD_TO, "arguments": {"source": ALFHILD, "target": "Zachary",
                                                                "location": "Battle-Born Farm"}}]


def test_the_whole_alfhild_talk_from_the_log_ends_with_her_leading():
    """The lines as they were said on 2026-11-10 (her replies split into sentences)."""
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    p.learn_bio(ALFHILD, BIO)
    out, _ = play(p, [
        ("P", "Where are you headed?"),
        ("N", "I'm just tending to things around the farm and making sure everything is in order."),
        ("P", "And which form might that be?"),
        ("N", "I run the Battle-Born Farm, where we grow leeks, wheat, and gourds."),
        ("P", "So I've seen it. It's not my favorite work, but I'll help you if you need it."),
        ("N", "Thank you, Zachary! That would be a great help."),
        ("P", "Of course, let's do this."),
        ("N", "Great! I appreciate your eagerness to help."),
        ("N", "Let's head to the farm and get started."),
        ("N", "Follow me!"),
        ("P", "Lead the way, I'm following you."),
        ("N", "Come on, I'll lead the way!"),
    ])
    assert any(a.get("identifier") == LEAD_TO for _, acts, _ in out for a in acts)


def test_the_farm_where_we_are_is_not_asked_about():
    p = TripPlanner(here="Battle-Born Farm", player_name="Zachary")
    p.learn_bio(ALFHILD, BIO)
    out, _ = play(p, [("P", "Ready."), ("N", "Let's head to the farm.")])
    assert out[0][2] == []


def test_no_place_words_and_nothing_named_asks_nothing():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, _ = play(p, [("P", "Hello."), ("N", "Follow me!")])
    assert out[0][2] == []


def test_right_behind_you_after_follow_me_asks_about_following():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, notes = play(p, [("P", "Come on, follow me."), ("N", "Alright, I'm right behind you, Zachary."),
                          ("P", "Yes."), ("N", "Let's see what's out there.")], npc="Lydia")
    assert out[0][2] == ["Have Lydia follow you? Say yes or no."]
    assert "Lydia now follows Zachary" in notes[1]
    assert out[1][1] == [{"identifier": FOLLOW, "arguments": {"source": ["Lydia"]}}]


def test_follow_question_needs_follow_offered_and_the_player_asking():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, _ = play(p, [("P", "Come on, follow me."), ("N", "I'm right behind you.")], offered=(LEAD_TO,), npc="Lydia")
    assert out[0][2] == []
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, _ = play(p, [("P", "How are you?"), ("N", "I'm right behind you.")], npc="Lydia")
    assert out[0][2] == []


def test_no_to_following_is_not_asked_again():
    p = TripPlanner(here="Whiterun", player_name="Zachary")
    out, notes = play(p, [("P", "Follow me."), ("N", "Right behind you."), ("P", "No."),
                          ("P", "Follow me."), ("N", "Right behind you.")], npc="Lydia")
    assert "should not follow" in notes[1]
    assert out[1][2] == []


def test_place_helpers():
    assert [x.name for x in places_in(BIO)] == ["Battle-Born Farm"]
    assert generic_words("Let's head to the farm, then home.") == ["farm", "home"]
    assert generic_words("The farm is nice") == ["farm"]


# ---- work: Whiterun templates, givers by form id, agreeing in words ----

def test_whiterun_has_harvest_work_with_givers_by_form_id():
    t = by_key("battleborn_harvest")
    assert t is not None and t.reward_gold > 0 and t.asks_player and t.deadline_days is None
    assert form_ref(t.giver) == ("Skyrim.esm", "0x00013BB0")
    assert by_key("pelagia_harvest") is not None
    assert form_ref("gerdur") is None and form_ref("Skyrim.esm:zz") is None
    assert {x.key for x in templates()} >= {"mill_firewood", "battleborn_harvest", "pelagia_harvest"}


@pytest.mark.parametrize("said, npc_last, offers", [
    ("So I've seen it. It's not my favorite work, but I'll help you if you need it.", None, True),
    ("I can lend a hand.", None, True),
    ("Count me in.", None, True),
    ("Of course.", "Would you help me with the harvest?", True),
    ("Of course.", "Where are you from?", False),
    ("I can't help you.", None, False),
    ("I won't help you.", None, False),
    ("I will not help.", None, False),
    ("Where are you headed?", None, False),
])
def test_offers_of_help(said, npc_last, offers):
    assert words.player_offers(said, npc_last) is offers


@pytest.mark.parametrize("line, takes", [
    ("Thank you, Zachary! That would be a great help.", True),
    ("No need, I can manage.", False),
    ("Are you sure?", False),
])
def test_npc_takes_it_up(line, takes):
    assert words.npc_takes_it_up(line) is takes


def _harvest_talk(tmp_path, offer, reply, rules=()):
    llm = ScriptedLLM(rules=[when_player_says(offer.split()[0].lower(), reply), *rules], replies=["Good day."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.alfhild()], location="Whiterun", game_days=3.4, time=12)
        s.continue_until_player_turn()
        s.say(offer)
        s.end(game_days=3.41)
        LivingWorld.latest.wait_for_jobs()
        keys = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    db.row_factory = sqlite3.Row
    task = db.execute("SELECT * FROM v_task WHERE template = 'battleborn_harvest' ORDER BY task_id DESC LIMIT 1").fetchone()
    promise = db.execute("SELECT COUNT(*) FROM obligation WHERE kind = 'promise'").fetchone()[0]
    prompt = llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""
    return task, promise, keys, prompt


def test_alfhild_offers_the_harvest_and_a_spoken_yes_takes_it(living_world_on, tmp_path):
    task, promise, keys, prompt = _harvest_talk(
        tmp_path, "So I've seen it. It's not my favorite work, but I'll help you if you need it.",
        "Thank you, Zachary! That would be a great help with the harvest.")
    assert "help bring in the harvest at Battle-Born Farm" in prompt
    assert task is not None and task["state"] == "accepted"
    assert promise == 1
    hints = [h for k in keys for h in (k.get(KEY_HINTS) or [])]
    assert "Agreed with Alfhild: help bring in the harvest at Battle-Born Farm." in hints


def test_a_refusal_in_words_takes_nothing(living_world_on, tmp_path):
    task, promise, _, _ = _harvest_talk(tmp_path, "I'll help you with the harvest.", "No need, I can manage.")
    assert task is not None and task["state"] != "accepted" and promise == 0


def test_task_words_switched_off(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv(words.WORDS_ENV, "0")
    task, promise, _, _ = _harvest_talk(tmp_path, "I'll help you with the harvest.", "Thank you!")
    assert promise == 0 and (task is None or task["state"] != "accepted")
