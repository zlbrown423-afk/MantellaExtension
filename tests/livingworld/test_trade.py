"""Trade: an agreed deal ("100 gold for two amethysts") swaps goods and gold; the game does the moving."""
import json

import pytest

from livingworld import actions as lw_actions
from livingworld.actions import trade
from livingworld.integration import ENV_VAR, LivingWorld
from src.actions.function_manager import FunctionManager


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


def test_the_guard_deal():
    args = trade.normalize({"source": "Whiterun Guard", "player_gives_item": "two amethysts", "npc_gives_gold": "100 gold"})
    assert args["player_gives_item"] == "amethysts" and args["player_gives_count"] == 2
    assert args["npc_gives_gold"] == 100 and args["player_gives_gold"] == 0
    assert "Amethyst" in args["player_gives_match"].split("|")
    assert args["npc_gives_item"] == "" and args["npc_gives_match"] == ""
    assert args["trade_ok"]


def test_gold_named_as_an_item_counts_as_gold():
    args = trade.normalize({"source": "G", "player_gives_item": "100 septims", "npc_gives_item": "iron daggers", "npc_gives_count": 3})
    assert args["player_gives_item"] == "" and args["player_gives_gold"] == 100
    assert args["npc_gives_count"] == 3 and "Iron Dagger" in args["npc_gives_match"].split("|")


def test_explicit_count_wins_and_limits_hold():
    args = trade.normalize({"source": "G", "player_gives_item": "2 amethyst", "player_gives_count": 3, "npc_gives_gold": 10**9})
    assert args["player_gives_count"] == 3
    assert args["npc_gives_gold"] == trade.MAX_GOLD


def test_gold_both_ways_nets_out():
    args = trade.normalize({"source": "G", "player_gives_gold": 30, "npc_gives_gold": 100})
    assert args["player_gives_gold"] == 0 and args["npc_gives_gold"] == 70


def test_empty_deal_is_flagged():
    assert not trade.normalize({"source": "G"})["trade_ok"]


def test_trade_is_offered_and_its_arguments_are_tidied(living_world_on):
    FunctionManager.load_all_actions()
    assert lw_actions.TRADE in FunctionManager._actions
    parsed = FunctionManager.parse_function_calls([_call("Trade", source="Whiterun Guard", player_gives_item="two amethysts", npc_gives_gold=100)])
    assert parsed and parsed[0]["identifier"] == lw_actions.TRADE
    args = parsed[0]["arguments"]
    assert args["player_gives_count"] == 2 and args["npc_gives_gold"] == 100 and args["player_gives_match"]


def test_trade_can_be_switched_off(living_world_on, monkeypatch):
    monkeypatch.setenv(trade.TRADE_ENV, "0")
    FunctionManager.load_all_actions()
    assert lw_actions.TRADE not in FunctionManager._actions
    assert lw_actions.SIT in FunctionManager._actions


def test_barter_points_non_merchant_deals_to_trade(living_world_on):
    FunctionManager.load_all_actions()
    assert "Trade" in FunctionManager._actions["mantella_npc_barter"]["description"]
