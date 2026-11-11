"""Keep your word: an NPC who says "I'll sit and have a drink" gets the Sit and Drink actions, even
when the LLM forgot the 'Sit:' / 'Drink:' prefix (livingworld/dialogue/promises.py)."""
import pytest

from livingworld import actions as lw_actions
from livingworld.dialogue import promises
from livingworld.dialogue.promises import PromiseKeeper, promised_actions
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from src.actions.function_manager import FunctionManager

PLAYER = actors.player("Torvar")
SIT, DRINK = lw_actions.SIT, lw_actions.DRINK


@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(promises.PROMISES_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


@pytest.fixture(autouse=True)
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


@pytest.mark.parametrize("line, expected", [
    ("I'll sit and have a drink with you.", [SIT, DRINK]),
    ("I’ll sit and have a drink with you, if you don't mind.", [SIT, DRINK]),
    ("Let me take a seat.", [SIT]),
    ("Let's have a drink!", [DRINK]),
    ("I'll join you for a drink.", [DRINK]),
    ("Let me pour myself some mead.", [DRINK]),
    ("I'm going to sit by the fire and drink my ale.", [SIT, DRINK]),
    ("I'll pull up a stool.", [SIT]),
])
def test_promises_for_right_now_are_found(line, expected):
    assert promised_actions(line) == expected


@pytest.mark.parametrize("line", [
    "Shall I sit?",                                   # a question
    "Would you like a drink?",
    "I won't sit with the likes of you.",              # a refusal
    "I'll not have a drink today.",
    "I'll have a drink once my shift ends.",           # later, or only if
    "I'll sit down later.",
    "I'll sit if you buy the mead.",
    "Let me get you a drink.",                         # for someone else: that is OrderDrinks
    "I'll get us some drinks from Orgnar.",
    "Come, sit with me.",                              # asks the player, says nothing of herself
    "I sat there yesterday.",
    "Sven drinks too much.",
])
def test_no_promise_no_action(line):
    assert promised_actions(line) == []


class _Action:
    def __init__(self, identifier):
        self.identifier = identifier


def test_the_keeper_adds_each_action_once_and_only_if_offered():
    keeper = PromiseKeeper([_Action(SIT), _Action(DRINK)])
    first: list = []
    assert keeper.keep("Gerdur", "I'll sit with you.", first) == [SIT]
    assert first == [{'identifier': SIT, 'arguments': {'source': ['Gerdur'], 'furniture': 'seat'}}]
    second: list = []
    assert keeper.keep("Gerdur", "Let me sit and have a drink.", second) == [DRINK]
    assert second == [{'identifier': DRINK, 'arguments': {'source': 'Gerdur'}}]

    not_offered = PromiseKeeper([_Action(SIT)])
    assert not_offered.keep("Gerdur", "I'll have a drink.", []) == []


def test_an_action_the_reply_already_started_is_not_added_again():
    keeper = PromiseKeeper([_Action(SIT), _Action(DRINK)])
    keeper.note_actions([{'identifier': DRINK, 'arguments': {'source': 'Gerdur'}}])   # a tool call
    already = [{'identifier': SIT}]                                                   # "Sit:" prefix
    assert keeper.keep("Gerdur", "I'll sit and have a drink.", already) == []
    assert already == [{'identifier': SIT}]


def _gerdur_says(tmp_path, line):
    llm = ScriptedLLM(rules=[when_player_says("drink", line)], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.5)
        s.continue_until_player_turn()
        lines = s.say("Fancy a drink, Gerdur?")
        s.end(game_days=3.51)
    return [a for line in lines for a in line.actions]


def test_gerdur_sits_and_drinks_when_she_says_she_will(living_world_on, tmp_path):
    sent = _gerdur_says(tmp_path, "Gladly. I'll sit and have a drink with you.")
    assert {'identifier': SIT, 'arguments': {'source': ['Gerdur'], 'furniture': 'seat'}} in sent
    assert {'identifier': DRINK, 'arguments': {'source': 'Gerdur'}} in sent


def test_a_prefixed_reply_is_sent_once(living_world_on, tmp_path):
    sent = _gerdur_says(tmp_path, "Sit: Gladly, I'll sit down with you.")
    assert [a['identifier'] for a in sent].count(SIT) == 1


def test_switched_off(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv(promises.PROMISES_ENV, "0")
    assert _gerdur_says(tmp_path, "Gladly. I'll sit and have a drink with you.") == []
