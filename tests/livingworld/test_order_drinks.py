"""OrderDrinks: an NPC buys a round from the bartender and brings one back for the player.

The walking, paying and handing over is Papyrus (Mantella-Spell). Here: the action is offered only
with Living World on, its arguments reach the game, and the purchase record the game sends forms the
right memories without counting as drinking.
"""
import json
import sqlite3

import pytest

from livingworld import actions as lw_actions
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, purchase, seen
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perception import intoxication
from livingworld.perception.records import BadRecord, parse_record
from src.actions.function_manager import FunctionManager

PLAYER = actors.player("Torvar")


def orgnar():
    # placeholder form ids: a fourth, distinct NPC
    return actors.actor("Orgnar", 0x00013482, 0x0001348F, 0, "Nord", "MaleNord")


@pytest.fixture(autouse=True)
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


def _call(name: str, **arguments) -> dict:
    return {'function': {'name': name, 'arguments': json.dumps(arguments)}}


def round_record(payer="player", key="p1", hod_los=True):
    return purchase(actors.gerdur(), orgnar(), "Nord Mead", 2, 10, 3.6, payer=payer, party_json=PLAYER, key=key,
                    witnesses=[seen(actors.hod(), 5.0, los=hod_los)])


def run(tmp_path, records):
    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Hm."])) as s:
        s.start([PLAYER, actors.gerdur(), actors.hod()], game_days=3.6, location="Sleeping Giant Inn",
                context_extra={KEY_EVENTS: records})
        s.continue_until_player_turn()
        s.end(game_days=3.61)
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    db.row_factory = sqlite3.Row
    return db


def memory_of(db, name):
    rows = db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception'", (name,)).fetchall()
    return rows[0] if rows else None


# ---- the action ----

def test_order_drinks_is_offered_only_with_living_world(monkeypatch):
    FunctionManager.load_all_actions()
    action = FunctionManager._actions[lw_actions.ORDER_DRINKS]
    assert "refuse" in action['description'] and "willing" in action['prompt']
    assert action['parameters']['payer']['enum'] == ["player", "npc"]
    monkeypatch.delenv(ENV_VAR)
    FunctionManager.load_all_actions()
    assert lw_actions.ORDER_DRINKS not in FunctionManager._actions


def test_order_arguments_reach_the_game_even_for_a_bartender_not_in_the_conversation():
    """The bartender is free text: an unknown name must not drop the whole order (the game looks them up)."""
    FunctionManager.load_all_actions()
    call = _call('OrderDrinks', source='Gerdur', bartender='Orgnar', drink='mead', payer='npc')
    result = FunctionManager.parse_function_calls([call])
    assert result == [{'identifier': lw_actions.ORDER_DRINKS,
                       'arguments': {'source': 'Gerdur', 'bartender': 'Orgnar', 'drink': 'mead', 'payer': 'npc'}}]


def test_basic_mode_order(tmp_path):
    llm = ScriptedLLM(rules=[when_player_says("get us some drinks", "OrderDrinks: I'll get us a round.")],
                      replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.6)
        s.continue_until_player_turn()
        lines = s.say("Go get us some drinks from Orgnar.")
        s.end(game_days=3.61)
    assert any({'identifier': lw_actions.ORDER_DRINKS} in line.actions for line in lines)


# ---- the purchase record ----

def test_purchase_record_parses():
    record = parse_record(round_record(payer="NPC"))   # Papyrus may recase literals
    assert (record.type, record.item, record.quantity, record.gold, record.payer) == ("purchase", "Nord Mead", 2, 10, "npc")
    assert record.target.name == "Orgnar" and record.party.name == "Torvar"


def test_purchase_record_needs_a_seller_and_payer():
    with pytest.raises(BadRecord):
        parse_record(round_record().replace("|payer=player", ""))
    with pytest.raises(BadRecord):
        parse_record(round_record().replace("|target=", "|seller="))


def test_buyer_seller_and_witness_remember_the_round(tmp_path):
    db = run(tmp_path, [round_record()])
    gerdur = memory_of(db, "Gerdur")
    assert gerdur["content"] == ("I bought 2 Nord Mead from Orgnar for 10 gold in Sleeping Giant Inn,"
                                 " a round for Torvar and me. Torvar paid.")
    assert gerdur["source_kind"] == "experienced"
    assert "Gerdur bought 2 Nord Mead from me for 10 gold" in memory_of(db, "Orgnar")["content"]
    hod = memory_of(db, "Hod")
    assert "Gerdur buy 2 Nord Mead from Orgnar for Gerdur and Torvar" in hod["content"]
    assert hod["source_kind"] == "witnessed"
    event = db.execute("SELECT type, payload_json FROM world_event WHERE game_key = 'p1'").fetchone()
    assert event["type"] == "trade"
    assert json.loads(event["payload_json"])["payer"] == "player"


def test_a_treat_is_remembered_as_one(tmp_path):
    db = run(tmp_path, [round_record(payer="npc")])
    gerdur = memory_of(db, "Gerdur")
    assert gerdur["content"].endswith("a round for Torvar and me. I paid.")
    assert gerdur["interpretation"] == "My treat for Torvar."


def test_buying_a_round_is_not_drinking_it(tmp_path):
    db = run(tmp_path, [round_record()])
    npc_id = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()[0]
    state = intoxication.drink_state(db, npc_id, 3.61)
    assert (state.recent_drinks, state.level, state.stage) == (0, 0, None)


def test_a_witness_without_line_of_sight_remembers_nothing(tmp_path):
    db = run(tmp_path, [round_record(hod_los=False)])
    assert memory_of(db, "Hod") is None
