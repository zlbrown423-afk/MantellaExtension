"""Body language BL1: proximity and gaze.

Game records are built as the Living World Papyrus writes them (``harness/perception.presence``); everything
after that is the real code path. ``harness/bodysim.py`` models the game's reaction rules to check that
reactions settle instead of feeding each other.
"""
import json
import sqlite3

import pytest

from livingworld.bodylang import presence as P
from livingworld.bodylang.state import Feelings
from livingworld.harness import actors, bodysim
from livingworld.harness.perception import KEY_EVENTS, presence
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_BODY, KEY_BODY_ON, KEY_MOOD, LivingWorld
from livingworld.perception.records import BadRecord
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def talk_replies(session):
    return [reply[comm_consts.KEY_REPLYTYPE_NPCTALK] for _, reply in session.exchanges
            if reply.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_NPCTALK
            and reply.get(comm_consts.KEY_REPLYTYPE_NPCTALK)]


def talk(tmp_path, records, day=3.5, replies=("Hm.",), more=()):
    llm = ScriptedLLM(replies=list(replies))
    with HeadlessSession(str(tmp_path), llm) as s:
        start = s.start([PLAYER, actors.gerdur()], game_days=day, context_extra={KEY_EVENTS: records})
        s.continue_until_player_turn()
        first = talk_replies(s)
        for text in more:
            s.say(text)
        later = talk_replies(s)[len(first):]
        s.end(game_days=day + 0.01)
    return llm, start, first, later


# ---- records ----
def test_record_round_trip_and_recased_kind():
    raw = presence(actors.gerdur(), "Crowd", 3.5, distance_m=0.6, seconds=4.0, count=2)
    p = P.parse(raw)
    assert (p.what, p.distance_m, p.duration_s, p.count, p.target.name, p.actor.is_player) == \
        ("crowd", 0.6, 4.0, 2, "Gerdur", True)
    assert P.is_presence(raw)


def test_records_between_two_npcs_are_refused():
    """Loop guard: only the player's behaviour is a percept, never one NPC's reaction to another."""
    raw = presence(actors.gerdur(), "crowd", 3.5, player_json=actors.hod())
    with pytest.raises(BadRecord):
        P.parse(raw)
    with pytest.raises(BadRecord):
        P.parse(presence(actors.gerdur(), "dance", 3.5))


# ---- appraisal: different people take the same thing differently ----
def test_same_crowding_different_people():
    p = P.parse(presence(actors.gerdur(), "crowd", 3.5, distance_m=0.6, seconds=4.0))
    prickly = P.appraise(p, {"personal_space": 0.85, "temper": 0.8, "anxiety": 0.4}, P.Bond())
    easy = P.appraise(p, {"personal_space": 0.2, "temper": 0.2, "anxiety": 0.3}, P.Bond())
    friend = P.appraise(p, {"personal_space": 0.85, "temper": 0.8, "anxiety": 0.4}, P.Bond(affinity=0.5))
    assert prickly["irritation"] > easy["irritation"] * 2
    assert friend["irritation"] < prickly["irritation"] / 2


def test_repeated_crowding_counts_for_more():
    once = P.appraise(P.parse(presence(actors.gerdur(), "crowd", 3.5, count=1)), {}, P.Bond())
    thrice = P.appraise(P.parse(presence(actors.gerdur(), "crowd", 3.5, count=3)), {}, P.Bond())
    assert thrice["unease"] == pytest.approx(once["unease"] * 2, rel=0.01)


def test_weapon_frightens_the_timid_more_than_the_bold():
    p = P.parse(presence(actors.gerdur(), "weapon", 3.5, distance_m=3.0))
    assert P.appraise(p, {"bold": 0.2}, P.Bond())["fear"] > 2 * P.appraise(p, {"bold": 0.9}, P.Bond())["fear"]


def test_profile_follows_personality_and_feelings():
    calm = Feelings()
    rattled = Feelings()
    rattled.add({"fear": 0.6, "irritation": 0.5, "crowded": 2})
    shy = P.profile("A", {"anxiety": 0.8, "warmth": 0.4, "dominance": 0.2, "personal_space": 0.7}, P.Bond(), calm)
    bold = P.profile("B", {"anxiety": 0.2, "warmth": 0.7, "dominance": 0.9, "bold": 0.9, "personal_space": 0.4},
                     P.Bond(), calm)
    assert shy.gaze < 0.35 < 0.6 <= bold.gaze           # shy looks away, bold holds the gaze
    assert shy.comfort_m > bold.comfort_m
    assert shy.steps_back and not bold.steps_back
    upset = P.profile("A", {"anxiety": 0.8, "personal_space": 0.7}, P.Bond(), rattled)
    assert upset.comfort_m > shy.comfort_m and upset.guard >= 0.6
    assert upset.wire().startswith("A|")


def test_mood_tag_is_advice_not_the_truth():
    calm = Feelings()
    assert P.clamp_mood_score(10, 4, calm) == 6           # at most 2 points from the last mood
    assert P.clamp_mood_score(9, None, calm) == 9         # nothing to compare with
    scared = Feelings()
    scared.add({"fear": 0.8})
    assert P.clamp_mood_score(9, None, scared) <= 4       # a frightened NPC is not delighted


# ---- through Mantella ----
def test_crowding_reaches_the_prompt_once_and_changes_feelings(tmp_path):
    records = [presence(actors.gerdur(), "approach", 3.499),
               presence(actors.gerdur(), "crowd", 3.4995, distance_m=0.6, seconds=5.0, count=2)]
    llm, start, first, later = talk(tmp_path, records, replies=["Hm.", "Well?"], more=["Hello."])
    assert start[KEY_BODY_ON] is True
    prompt = llm.dialogue_calls[0].system_prompt
    assert "What Gerdur noticed Torvar doing just now" in prompt
    assert "Torvar stood very close, 0.6 m away, for about 5 seconds (that is 2 times" in prompt
    assert "Torvar walked straight up" in prompt
    everything = json.dumps(llm.dialogue_calls[-1].messages)
    assert everything.count("stood very close") == 1                          # told once, not again per turn
    profiles = [p for r in first for p in r.get(KEY_BODY, [])]
    assert len(profiles) == 1 and profiles[0].startswith("Gerdur|")
    assert not any(KEY_BODY in r for r in later)                            # unchanged, not sent again
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    (mood_json,) = db.execute("SELECT e.mood_json FROM emotion_state e JOIN npc n USING (npc_id)"
                              " WHERE n.display_name = 'Gerdur'").fetchone()
    body = json.loads(mood_json)["body"]
    assert body["crowded"] > 0 and body["unease"] > 0


def test_body_language_during_a_conversation_reaches_the_next_line(tmp_path):
    llm = ScriptedLLM(replies=["Hm.", "Well?", "Yes?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.5)
        s.continue_until_player_turn()
        s.say("Hello.", context_extra={KEY_EVENTS: [presence(actors.gerdur(), "crowd", 3.5002, distance_m=0.5,
                                                              seconds=3.0)]})
        s.say("Nice day.")
        s.end(game_days=3.51)
    user_lines = [m["content"] for m in llm.dialogue_calls[-1].messages if m["role"] == "user"]
    noticed = [u for u in user_lines if "Gerdur noticed: Torvar stood very close" in u]
    assert len(noticed) == 1 and "Hello." in noticed[0]


def test_same_record_twice_is_counted_once(tmp_path):
    rec = presence(actors.gerdur(), "crowd", 3.4995, distance_m=0.6, seconds=5.0, key="same")
    llm, *_ = talk(tmp_path, [rec, rec])
    assert llm.dialogue_calls[0].system_prompt.count("stood very close") == 1


def test_old_observations_are_not_told(tmp_path):
    rec = presence(actors.gerdur(), "crowd", 3.40, distance_m=0.6, seconds=5.0)   # ~2.4 game hours earlier
    llm, *_ = talk(tmp_path, [rec])
    assert "stood very close" not in llm.dialogue_calls[0].system_prompt


def test_mood_keeps_body_feelings_and_is_clamped_by_fear(tmp_path):
    records = [presence(actors.gerdur(), "weapon", 3.4995, distance_m=2.0, key=f"w{i}") for i in range(4)]
    llm, start, first, later = talk(tmp_path, records, replies=["<mood delighted 10> Lovely day!"])
    meter = [r[KEY_MOOD] for r in first if KEY_MOOD in r][0]
    score = meter.count("=")
    assert score <= 5
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    (mood_json,) = db.execute("SELECT e.mood_json FROM emotion_state e JOIN npc n USING (npc_id)"
                              " WHERE n.display_name = 'Gerdur'").fetchone()
    data = json.loads(mood_json)
    assert data["score"] == score and data["body"]["fear"] > 0.5     # mood.save kept the body feelings


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_BODY_LANGUAGE", "0")
    rec = presence(actors.gerdur(), "crowd", 3.4995, distance_m=0.6, seconds=5.0)
    llm, start, first, _ = talk(tmp_path, [rec], replies=["<mood delighted 10> Hi."])
    assert start[KEY_BODY_ON] is False
    assert "stood very close" not in llm.dialogue_calls[0].system_prompt
    assert not any(KEY_BODY in r for r in first)


# ---- the game's reaction rules settle (model of the Papyrus loop) ----
def test_player_standing_still_too_close_settles():
    gerdur = bodysim.SimNpc("Gerdur", x=0.8, comfort=1.1, guard=0.7)
    hod = bodysim.SimNpc("Hod", x=-0.9, comfort=1.0, guard=0.2)
    bodysim.run([gerdur, hod], lambda t: 0.0, seconds=120)
    for npc in (gerdur, hod):
        assert npc.steps == 1                                   # one step back, then calm
        assert [r for _, r in npc.records] == ["crowd"]          # her own step never reads as the player leaving
        assert abs(npc.x) > npc.comfort                          # ended outside the comfort distance
    assert gerdur.arms == 1 and hod.arms == 0


def test_player_following_is_bounded():
    """Even if the player keeps closing in, reactions stay under the cooldowns and the per-minute cap."""
    gerdur = bodysim.SimNpc("Gerdur", x=0.8, comfort=1.1, guard=0.7)
    bodysim.run([gerdur], lambda t: gerdur.x - 0.5, seconds=60)
    assert gerdur.steps <= 6                                    # 10 s step cooldown over 60 s
    assert len(gerdur.records) <= 12


def test_player_stepping_back_is_noticed_once():
    gerdur = bodysim.SimNpc("Gerdur", x=0.8, comfort=1.1, steps_back=False)
    bodysim.run([gerdur], lambda t: 0.0 if t < 5 else -1.5, seconds=30)
    assert [r for _, r in gerdur.records] == ["crowd", "backed_off"]


def test_npcs_never_step_toward_the_player():
    npcs = [bodysim.SimNpc(f"N{i}", x=x, comfort=1.2) for i, x in enumerate((0.5, -0.4, 1.0))]
    start = [abs(n.x) for n in npcs]
    bodysim.run(npcs, lambda t: 0.0, seconds=60)
    assert all(abs(n.x) >= s for n, s in zip(npcs, start))
