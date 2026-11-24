"""Cavalry orders by voice: "Mount up!" in a conversation reaches the game as mantella_lw_cavalry_order."""
import pytest

from livingworld.dialogue import cavalry_orders as CO
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import jobs


@pytest.mark.parametrize("text, orders", [
    ("Mount up!", [CO.MOUNT]),
    ("Riders, mount up.", [CO.MOUNT]),
    ("Saddle up, we ride for Rorikstead.", [CO.MOUNT]),
    ("To horse!", [CO.MOUNT]),
    ("Get on your horses.", [CO.MOUNT]),
    ("Dismount.", [CO.DISMOUNT]),
    ("Everyone, get off your horses.", [CO.DISMOUNT]),
    ("Fall in!", [CO.COLUMN]),
    ("Fall in line.", [CO.COLUMN]),
    ("Form a column behind me.", [CO.COLUMN]),
    ("Single file through the pass.", [CO.COLUMN]),
    ("Line abreast!", [CO.LINE]),
    ("Fall in line abreast.", [CO.LINE]),
    ("Form a line.", [CO.LINE]),
    ("At ease.", [CO.EASE]),
    ("Break formation.", [CO.EASE]),
    ("Rally to me!", [CO.RALLY_ME]),
    ("Riders, to me!", [CO.RALLY_ME]),
    ("Rally at the hall.", [CO.RALLY_HALL]),
    ("Head back to the stables and wait.", [CO.RALLY_HALL]),
    ("Rally at Greymarch.", [CO.RALLY_HALL]),
    ("Riders, back to Greymarch.", [CO.RALLY_HALL]),
    ("Mount up and fall in.", [CO.MOUNT, CO.COLUMN]),
    ("Dismount. At ease.", [CO.DISMOUNT, CO.EASE]),
    ("Mount up, then line abreast, then at ease, then dismount.", [CO.MOUNT, CO.LINE, CO.EASE]),
    ("Let's mount up.", [CO.MOUNT]),
    ("Alright Lydia, mount up.", [CO.MOUNT]),
])
def test_orders(text, orders):
    assert CO.orders_in(text) == orders


@pytest.mark.parametrize("text", [
    "Should we mount up?",
    "Can you dismount for me",
    "Don't dismount yet.",
    "Do not fall in.",
    "When we reach the gate, dismount.",
    "Dismount when we get to the gate.",
    "I told them to fall in.",
    "Remember to mount up later.",
    "I nearly fell in the river.",
    "Don't fall in the river.",
    "I could fall in love with this place.",
    "We should mount up soon.",
    "The hall looks good.",
    "Greymarch looks good.",
    "I love Greymarch.",
    "How do I mount a horse?",
    "",
])
def test_not_orders(text):
    assert CO.orders_in(text) == []


def test_wire_one_digit_per_order():
    assert CO.wire([CO.MOUNT]) == 1
    assert CO.wire([CO.MOUNT, CO.COLUMN]) == 13
    assert CO.wire([1, 2, 3, 4]) == 123


def test_voice_reads_a_line_once_and_sends_once():
    voice = CO.CavalryVoice()
    assert voice.heard("Mount up!") == [CO.MOUNT]
    assert voice.heard("Mount up!") == []          # the same line offered twice
    assert len(voice.take_notes()) == 1 and voice.take_notes() == []
    assert voice.take_keys() == {CO.KEY_ORDER: 1}
    assert voice.take_keys() == {}


def test_off_switch(monkeypatch):
    monkeypatch.setenv(CO.ENV, "0")
    assert not CO.enabled()
    monkeypatch.delenv(CO.ENV)
    assert CO.enabled()


@pytest.fixture
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def _converse(work_dir, say, monkeypatch, off=False):
    if off:
        monkeypatch.setenv(CO.ENV, "0")
    else:
        monkeypatch.delenv(CO.ENV, raising=False)
    llm = ScriptedLLM(replies=["Aye.", "As you command."])
    sent = []
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.actor("Lydia", 0x000A2C94, 0x000A2C8E, 1, "Nord", "FemaleEvenToned")],
                location="Whiterun", game_days=3.0)
        lw = LivingWorld.latest
        original = lw.line_reply_keys

        def capture(sentence):
            keys = original(sentence)
            sent.append(dict(keys))
            return keys
        lw.line_reply_keys = capture
        s.continue_until_player_turn()
        s.say(say)
        s.continue_until_player_turn()
        s.end(game_days=3.01)
    return [k[CO.KEY_ORDER] for k in sent if CO.KEY_ORDER in k], llm


def test_spoken_order_goes_to_the_game_once(tmp_path, monkeypatch, living_world_inline):
    orders, llm = _converse(tmp_path, "Mount up and fall in.", monkeypatch)
    assert orders == [13]
    assert "gave their riders the order: mount up, then fall in (column)" in " ".join(c.all_text for c in llm.calls if c.kind == "stream")


def test_small_talk_sends_nothing(tmp_path, monkeypatch, living_world_inline):
    orders, _ = _converse(tmp_path, "Should we mount up later?", monkeypatch)
    assert orders == []


def test_switched_off_sends_nothing(tmp_path, monkeypatch, living_world_inline):
    orders, _ = _converse(tmp_path, "Mount up!", monkeypatch, off=True)
    assert orders == []
