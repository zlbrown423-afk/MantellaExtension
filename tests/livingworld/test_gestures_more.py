"""Handshake, salute and the adult-only gestures (``livingworld/bodylang/presence.py``), each behind its own switch."""
import pytest

from livingworld.bodylang import presence as P
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, presence
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_ADULT_GESTURES_ON, KEY_GESTURES_ON, KEY_HANDSHAKE_ON,
                                     KEY_SALUTE_ON, LivingWorld)
from livingworld.perception.records import parse_record

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    for env in ("MANTELLA_LW_GESTURES", "MANTELLA_LW_HANDSHAKE", "MANTELLA_LW_SALUTE", "MANTELLA_LW_ADULT_GESTURES"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def talk(tmp_path, records, who=None, day=3.5):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        start = s.start([PLAYER, who or actors.gerdur()], game_days=day, context_extra={KEY_EVENTS: records})
        s.continue_until_player_turn()
        s.end(game_days=day + 0.01)
    return llm.dialogue_calls[0].system_prompt, start


def test_groups():
    assert P.gesture_group(presence(actors.gerdur(), "wave", 3.5)) == "gestures"
    assert P.gesture_group(presence(actors.gerdur(), "Handshake", 3.5)) == "handshake"
    assert P.gesture_group(presence(actors.gerdur(), "salute", 3.5)) == "salute"
    assert P.gesture_group(presence(actors.gerdur(), "stroke", 3.5)) == "adult"
    assert P.gesture_group(presence(actors.gerdur(), "point_down", 3.5)) == "adult"
    assert P.gesture_group(presence(actors.gerdur(), "point", 3.5)) == "gestures"   # not point_down
    assert P.gesture_group(presence(actors.gerdur(), "crowd", 3.5)) is None
    assert not P.is_gesture_record(presence(actors.gerdur(), "point_down", 3.5))


def test_handshake_and_salute_words():
    shook = P.parse(presence(actors.gerdur(), "handshake", 3.5, extra={"back": 1}))
    assert P.observation(shook, "Torvar") == "Torvar held out a hand to shake, and Gerdur offered theirs in return."
    left = P.parse(presence(actors.gerdur(), "handshake", 3.5, extra={"back": 0}))
    assert P.observation(left, "Torvar") == "Torvar held out a hand to shake; Gerdur did not take it."
    assert P.appraise(shook, {}, P.Bond())["unease"] < 0 and P.appraise(left, {}, P.Bond()) == {}
    salute = P.parse(presence(actors.gerdur(), "salute", 3.5, extra={"back": 1, "style": "Chest"}))
    assert P.observation(salute, "Torvar") == "Torvar saluted Gerdur (a fist over the heart). Gerdur returned the salute."


def test_adult_words_leave_the_reaction_to_the_npc():
    p = P.parse(presence(actors.gerdur(), "stroke", 3.5, count=2, extra={"naked": 1}))
    text = P.observation(p, "Torvar")
    assert "stroking motion" in text and "bare manhood" in text and "again and again" in text
    assert "obliges Gerdur to nothing" in text and "not an invitation" in text
    point = P.observation(P.parse(presence(actors.gerdur(), "point_down", 3.5)), "Torvar")
    assert "his groin" in point and "eyes followed" in point


def test_adult_feelings_follow_modesty_and_closeness():
    stroke = P.parse(presence(actors.gerdur(), "stroke", 3.5))
    prim = P.appraise(stroke, {"modesty": 0.9}, P.Bond())
    easy = P.appraise(stroke, {"modesty": 0.1}, P.Bond())
    close = P.appraise(stroke, {"modesty": 0.9}, P.Bond(affinity=0.6))
    assert prim["embarrassment"] > easy["embarrassment"] > 0 and "irritation" in prim
    assert close["embarrassment"] < prim["embarrassment"] and "unease" not in close
    hostile = P.appraise(stroke, {}, P.Bond(trust=-0.5))
    assert hostile["irritation"] > 0 and "embarrassment" not in hostile
    assert P.appraise(P.parse(presence(actors.gerdur(), "point_down", 3.5)), {"modesty": 0.9}, P.Bond())[
        "embarrassment"] < prim["embarrassment"]


def test_switches_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GESTURES", "0")   # the PC launcher's setting: BL2 off, the new ones still go
    recs = [presence(actors.gerdur(), "wave", 3.4995), presence(actors.gerdur(), "handshake", 3.4995, extra={"back": 1}),
            presence(actors.gerdur(), "salute", 3.4995), presence(actors.gerdur(), "stroke", 3.4995)]
    prompt, start = talk(tmp_path, recs)
    assert start[KEY_GESTURES_ON] is False and start[KEY_HANDSHAKE_ON] is True and start[KEY_SALUTE_ON] is True
    assert start[KEY_ADULT_GESTURES_ON] is False
    assert "waved" not in prompt and "stroking" not in prompt
    assert "held out a hand to shake" in prompt and "Torvar saluted Gerdur." in prompt


def test_adult_switch_on(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_ADULT_GESTURES", "1")
    monkeypatch.setenv("MANTELLA_LW_HANDSHAKE", "0")
    prompt, start = talk(tmp_path, [presence(actors.gerdur(), "stroke", 3.4995),
                                    presence(actors.gerdur(), "handshake", 3.4995)])
    assert start[KEY_ADULT_GESTURES_ON] is True and start[KEY_HANDSHAKE_ON] is False
    assert "stroking motion" in prompt
    assert "held out a hand" not in prompt


def test_children_never_hear_of_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_ADULT_GESTURES", "1")
    child = actors.actor("Dorthe", 0x0001347E, 0x0001348B, 1, "NordChild", "FemaleChild")
    prompt, _ = talk(tmp_path, [presence(child, "point_down", 3.4995)], who=child)
    assert "pointed down" not in prompt


def test_handshake_grab_is_remembered_as_one():
    from livingworld.perception.records import _consent
    assert _consent("Handshake") == "handshake"
    from livingworld.perception import formation as F
    assert "handshake" in F._AFFECTION
