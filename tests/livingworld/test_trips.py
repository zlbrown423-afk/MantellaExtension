"""Trips: an NPC who agrees to go somewhere sets off once the player says yes
(livingworld/dialogue/trips.py)."""
import pytest

from livingworld.dialogue import trips
from livingworld.dialogue.trips import LEAD_TO, TRAVEL_TO, TripPlanner, TripWatcher, find_places, npc_agrees_to_go
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_HINTS, LivingWorld
from src.actions.function_manager import FunctionManager
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Zachary")


def names(found):
    return [p.name for p in found]


@pytest.mark.parametrize("said, expected", [
    ("Let's go to battleborn farm.", ["Battle-Born Farm"]),        # what speech-to-text wrote in game
    ("Yes, let's go to Brees home.", ["Breezehome"]),
    ("take me to the bannered mayor", ["The Bannered Mare"]),
    ("let's go to river would", ["Riverwood"]),
    ("let's go to the bee and bob", ["The Bee and Barb"]),
    ("go to the battle born house", ["Battle-Born Farm", "House of Clan Battle-Born"]),   # misheard: choices
])
def test_places_are_found_loosely(said, expected):
    assert names(find_places(said, exclude=["Whiterun"])) == expected


@pytest.mark.parametrize("said", [
    "Let's go see what he's up to.",
    "Let's go pay hard of visit.",
    "lets go get a drink together",
    "let's go to white run",            # where we already are
])
def test_no_place_no_guess(said):
    assert find_places(said, exclude=["Whiterun"]) == []


@pytest.mark.parametrize("line", [
    "Alright, Zachary, let's head to Battle-Born Farm.",
    "Perfect! Let's get moving then.",
    "Alright, I'm right behind you, Zachary.",
    "Follow me.",
    "I'll take you there.",
])
def test_agreeing_to_set_off(line):
    assert npc_agrees_to_go(line)


@pytest.mark.parametrize("line", [
    "Shall we go?",
    "I won't go anywhere with you.",
    "I'll go once my shift ends.",
    "We can head out tomorrow.",
    "I can help you with that.",
    "The farm is just outside the gate.",
])
def test_not_agreeing(line):
    assert not npc_agrees_to_go(line)


class _Action:
    def __init__(self, identifier):
        self.identifier = identifier


def _planner():
    return TripPlanner(here="Whiterun", player_name="Zachary")


def test_yes_starts_lead_to_on_the_next_line():
    p = _planner()
    assert p.player_said("Let's go to battleborn farm.") is None
    w = TripWatcher(p, [], [LEAD_TO, TRAVEL_TO])
    line: list = []
    w.keep("Alfhild Battle-Born", "Alright, let's head to the farm together!", line)
    assert line == []                                   # nothing moves before the player's yes
    assert p.take_notices() == ["Go with Alfhild to Battle-Born Farm? Say yes or no."]
    note = p.player_said("Yes.")
    assert "Alfhild Battle-Born now leads Zachary to Battle-Born Farm" in note
    nxt: list = []
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Follow me, then.", nxt)
    assert nxt == [{"identifier": LEAD_TO, "arguments": {"source": "Alfhild Battle-Born", "target": "Zachary",
                                                         "location": "Battle-Born Farm"}}]
    later: list = []
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Let's keep going.", later)
    assert later == [] and p.take_notices() == []      # on her way: never asked again


def test_travel_to_when_lead_to_is_switched_off():
    p = _planner()
    p.player_said("Let's go to Riverwood.")
    TripWatcher(p, [], [TRAVEL_TO]).keep("Gerdur", "Let's be off.", [])
    p.player_said("yeah")
    line: list = []
    TripWatcher(p, [], [TRAVEL_TO]).keep("Gerdur", "Come on.", line)
    assert line == [{"identifier": TRAVEL_TO, "arguments": {"source": ["Gerdur"], "location": "Riverwood",
                                                            "leave_now": True}}]


def test_numbered_choices_when_the_name_is_unclear():
    p = _planner()
    p.player_said("let's go to the battle born house")
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Let's go.", [])
    assert p.take_notices() == ["Where to with Alfhild? Say one or two, or no:",
                                "1: Battle-Born Farm", "2: House of Clan Battle-Born"]
    assert p.player_said("yes") is None
    assert p.take_notices()[1:] == ["1: Battle-Born Farm", "2: House of Clan Battle-Born"]   # shown again
    assert "to House of Clan Battle-Born" in p.player_said("Two.")


def test_no_drops_it_for_this_conversation():
    p = _planner()
    p.player_said("Let's go to battleborn farm.")
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Let's head to Battle-Born Farm.", [])
    p.take_notices()
    assert "decided not to" in p.player_said("No, not now.")
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Let's head to Battle-Born Farm.", [])
    assert p.take_notices() == [] and p.go is None


def test_an_ignored_question_lapses():
    p = _planner()
    p.player_said("Let's go to battleborn farm.")
    TripWatcher(p, [], [LEAD_TO]).keep("Alfhild Battle-Born", "Let's go.", [])
    p.player_said("What do you grow there?")
    p.player_said("And who works it?")
    assert p.question is None and p.go is None


def test_no_question_when_the_ai_already_started_the_trip():
    p = _planner()
    p.player_said("Lead me to Riverwood.")
    line = [{"identifier": LEAD_TO, "arguments": {"source": "Gerdur", "target": "Zachary", "location": "Riverwood"}}]
    TripWatcher(p, [], [LEAD_TO]).keep("Gerdur", "Follow me.", line)
    assert p.take_notices() == []


def test_no_question_without_travel_actions():
    p = _planner()
    p.player_said("Let's go to Riverwood.")
    TripWatcher(p, [], []).keep("Gerdur", "Let's go.", [])
    assert p.take_notices() == []


# ---------------------------------------------------------------- through the whole server

@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(trips.TRIPS_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


@pytest.fixture(autouse=True)
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


def _walk_to_the_farm(tmp_path, answer):
    llm = ScriptedLLM(rules=[when_player_says("farm", "Alright, let's head to Battle-Born Farm.")],
                      replies=["Follow me, then."])
    with HeadlessSession(str(tmp_path), llm, settings={"disabled_actions": ""}) as s:
        s.start([PLAYER, actors.gerdur()], location="Whiterun", game_days=3.5)
        s.continue_until_player_turn()
        first = s.say("Let's go to battleborn farm.")
        second = s.say(answer)
        s.end(game_days=3.51)
        keys = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
    return first, second, keys, llm


def test_in_game_flow_yes(living_world_on, tmp_path):
    first, second, keys, llm = _walk_to_the_farm(tmp_path, "Yes.")
    assert not any(a for line in first for a in line.actions)
    hints = [h for k in keys for h in (k.get(KEY_HINTS) or [])]
    assert "Go with Gerdur to Battle-Born Farm? Say yes or no." in hints
    sent = [a for line in second for a in line.actions]
    assert {"identifier": LEAD_TO, "arguments": {"source": "Gerdur", "target": "Zachary",
                                                 "location": "Battle-Born Farm"}} in sent
    assert "Gerdur now leads Zachary to Battle-Born Farm" in llm.dialogue_calls[-1].last_user_text


def test_in_game_flow_no(living_world_on, tmp_path):
    _, second, _, _ = _walk_to_the_farm(tmp_path, "No.")
    assert not any(a for line in second for a in line.actions)


def test_switched_off(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv(trips.TRIPS_ENV, "0")
    _, second, _, _ = _walk_to_the_farm(tmp_path, "Yes.")
    assert not any(a for line in second for a in line.actions)
