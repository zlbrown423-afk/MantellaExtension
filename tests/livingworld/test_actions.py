"""Living World actions: Sit, StandUp and GoThere.

Offered to the LLM only with MANTELLA_LIVING_WORLD=1. The game side (finding furniture, the VR pointing
target) is Papyrus in Mantella-Spell and is tested in game.
"""
import json

import pytest

from livingworld import actions as lw_actions
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from src.actions.function_manager import FunctionManager

PLAYER = actors.player("Torvar")


@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


@pytest.fixture(autouse=True)
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


def _call(name: str, **arguments) -> dict:
    return {'function': {'name': name, 'arguments': json.dumps(arguments)}}


def test_actions_are_not_offered_without_living_world(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    FunctionManager.load_all_actions()
    assert not set(lw_actions.ALL) & set(FunctionManager._actions)
    assert 'mantella_npc_wait' in FunctionManager._actions   # upstream actions still load


def test_actions_load_with_living_world(living_world_on):
    FunctionManager.load_all_actions()
    for identifier in lw_actions.ALL:
        action = FunctionManager._actions[identifier]
        for field in ('name', 'description', 'key', 'prompt', 'requires_response', 'is-interrupting',
                      'one-on-one', 'multi-npc', 'radiant', 'allow_repeat'):
            assert field in action, f"{identifier} lacks {field}"
    names = [a['name'] for a in FunctionManager._actions.values()]
    assert len(names) == len(set(names)), "tool names must be unique"


def test_actions_can_be_disabled_like_upstream_ones(living_world_on):
    FunctionManager.load_all_actions(disabled_actions=['GoThere'])
    assert lw_actions.GO_THERE not in FunctionManager._actions
    assert lw_actions.SIT in FunctionManager._actions


def test_the_npc_may_refuse(living_world_on):
    """Refusing is not calling the action, so every description and basic-mode prompt says it is optional."""
    FunctionManager.load_all_actions()
    for identifier in (lw_actions.SIT, lw_actions.GO_THERE):
        action = FunctionManager._actions[identifier]
        assert "refuse" in action['description']
        assert "willing" in action['prompt']


def test_sit_call_passes_the_furniture_kind_to_the_game(living_world_on):
    FunctionManager.load_all_actions()
    result = FunctionManager.parse_function_calls([_call('Sit', source=['Gerdur'], furniture='bench')])
    assert result == [{'identifier': lw_actions.SIT, 'arguments': {'source': ['Gerdur'], 'furniture': 'bench'}}]


def test_sit_offers_pointing_and_bed(living_world_on):
    FunctionManager.load_all_actions()
    kinds = FunctionManager._actions[lw_actions.SIT]['parameters']['furniture']['enum']
    assert {'pointed', 'seat', 'bed'} <= set(kinds)


def test_sitting_again_next_turn_is_allowed(living_world_on):
    """'Sit there' then 'no, sit there' must not be filtered as a duplicate."""
    FunctionManager.load_all_actions()
    sit = _call('Sit', source=['Gerdur'], furniture='pointed')
    assert len(FunctionManager.parse_function_calls([sit])) == 1
    assert len(FunctionManager.parse_function_calls([sit])) == 1


def test_basic_mode_sends_sit_and_stand_up_to_the_game(living_world_on, tmp_path):
    """With advanced actions off (Mantella's default) the NPC answers 'Sit: ...' and the game gets the identifier."""
    llm = ScriptedLLM(rules=[
        when_player_says("have a seat", "Sit: Don't mind if I do."),
        when_player_says("stand up", "StandUp: Fine."),
    ], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.5)
        s.continue_until_player_turn()
        sit_lines = s.say("Have a seat, Gerdur.")
        stand_lines = s.say("Now stand up.")
        s.end(game_days=3.51)
    assert any({'identifier': lw_actions.SIT} in line.actions for line in sit_lines)
    assert any({'identifier': lw_actions.STAND_UP} in line.actions for line in stand_lines)

