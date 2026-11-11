"""Partners who catch each other mid-scene, guards and a naked player, and women's eyes on a naked player.

From the 2026-11-10 test: Hod saw Gerdur with the player from 2.4 m, the memory and jealousy were stored, and he
never said a word about it. These tests drive the game's records (as Mantella-Spell writes them) through the
real code path.
"""
import sqlite3

import pytest

from livingworld.bodylang import peek
from livingworld.bodylang import presence as P
from livingworld.bodylang.state import Feelings
from livingworld.guard import decency
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, presence, react, scene, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.intimacy import confront, scenes
from livingworld.intimacy.scenes import KEY_SCENE
from livingworld.perception.records import parse_record
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")
GUARD = actors.actor("Whiterun Guard", 0x0001A6A3, 0x000A0A44, 0, "Nord", "MaleNord")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BB0, 1, "Nord", "FemaleNord")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def make_spouses(db, a, b):
    for x, y in ((a, b), (b, a)):
        db.execute("INSERT INTO relationship (from_id, to_id, status, kinship, updated_gt) VALUES (?,?,?,?,?)"
                   " ON CONFLICT(from_id, to_id) DO UPDATE SET kinship = 'spouse', status = 'spouse'",
                   (npc_id(db, x), npc_id(db, y), "spouse", "spouse", 1.0))
    db.commit()


def talk(work_dir, npcs, day, events=None, live=None):
    llm = ScriptedLLM(replies=["Hm."])
    context = {KEY_EVENTS: events or []}
    if live is not None:
        context[KEY_SCENE] = live
    with HeadlessSession(str(work_dir), llm) as s:
        start = s.start([PLAYER, *npcs], game_days=day, context_extra=context)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt, start


# ---- partners who catch each other -------------------------------------------------------------------

def test_the_start_reply_lists_couples_for_the_game(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.4)
    make_spouses(open_db(tmp_path), "Gerdur", "Hod")
    _, start = talk(tmp_path, [actors.gerdur()], 3.45)
    assert start[confront.KEY_CONFRONT_ON] is True
    assert "Gerdur|Hod" in start[confront.KEY_COUPLES] or "Hod|Gerdur" in start[confront.KEY_COUPLES]


def test_hod_walking_in_confronts_and_gerdur_knows_she_is_caught(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.4)
    make_spouses(open_db(tmp_path), "Gerdur", "Hod")
    live = scene([PLAYER, actors.gerdur()], 3.5, acts=("kissing",), via="now", witnesses=[seen(actors.hod(), 2.4)])
    hod, _ = talk(tmp_path, [actors.hod()], 3.5, live=[live])
    assert "Hod has just walked in on Gerdur, Hod's own partner, being intimate with Torvar" in hod
    assert "Hod does not stay silent" in hod
    gerdur, _ = talk(tmp_path, [actors.gerdur(), actors.hod()], 3.5, live=[live])
    assert "Hod, Gerdur's own partner, has walked in and can see this. Gerdur has been caught" in gerdur


def test_a_stranger_watching_is_not_a_confrontation():
    live = scenes.SceneNow({10: "Torvar", 1: "Gerdur"}, ["kissing"], seen_by={3}, seer_names={3: "Sigrid"})
    lines = scenes.lines_for(1, "Gerdur", [live], {}, confront=True, watched_by=set())
    assert not any("caught" in line for line in lines)
    assert scenes.lines_for(3, "Sigrid", [live], {}, confront=True, partners=set()) == [
        "Sigrid can see Torvar and Gerdur being intimate close by (kissing)."]


def test_hod_does_not_let_it_go_afterwards(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.4)
    make_spouses(open_db(tmp_path), "Gerdur", "Hod")
    rec = scene([PLAYER, actors.gerdur()], 3.5, acts=("vaginalsex",), witnesses=[seen(actors.hod(), 2.4)],
                location="Hod and Gerdur's House")
    talk(tmp_path, [actors.gerdur()], 3.51, events=[rec])
    prompt, _ = talk(tmp_path, [actors.hod()], 3.7)   # about five hours later, the player is here
    assert "Hod saw Gerdur, Hod's own partner, being intimate with Torvar in Hod and Gerdur's House about 5 hours ago" \
        in prompt
    assert "with Torvar here Hod brings it up: Hod wants answers" in prompt
    later, _ = talk(tmp_path, [actors.hod()], 6.0)       # days later: still on his mind, no longer brought up
    assert "It still sits between them" in later
    much_later, _ = talk(tmp_path, [actors.hod()], 9.0)
    assert "Hod saw Gerdur" not in much_later


def test_confront_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(confront.ENV, "0")
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.4)
    make_spouses(open_db(tmp_path), "Gerdur", "Hod")
    live = scene([PLAYER, actors.gerdur()], 3.5, via="now", witnesses=[seen(actors.hod(), 2.4)])
    prompt, start = talk(tmp_path, [actors.hod()], 3.5, live=[live])
    assert start[confront.KEY_CONFRONT_ON] is False and confront.KEY_COUPLES not in start
    assert "walked in" not in prompt and "Hod reacts as Hod would to that" in prompt


# ---- guards and a naked player --------------------------------------------------------------------

def test_a_decency_record_is_accepted_without_a_victim():
    record = parse_record(react(GUARD, PLAYER, 3.5, via="decency", seconds=40.0))
    assert record.via == "decency" and record.party is None


def test_the_guard_warns_first_then_may_fine(tmp_path):
    first, start = talk(tmp_path, [GUARD], 3.5, events=[react(GUARD, PLAYER, 3.4995, via="decency")])
    assert start[decency.KEY_DECENCY_ON] is True
    assert "saw Torvar walking about naked in public and has come over about it" in first
    assert "a warning, no fine. Do not use ReportCrime" in first
    second, _ = talk(tmp_path, [GUARD], 3.8, events=[react(GUARD, PLAYER, 3.7995, via="decency")])
    assert "use ReportCrime with a bounty of 20 gold" in second


def test_a_grab_warning_is_not_a_decency_warning(tmp_path):
    prompt, _ = talk(tmp_path, [GUARD], 3.5, events=[react(GUARD, PLAYER, 3.4995, via="guard",
                                                           victim_json=actors.gerdur())])
    assert "naked in public" not in prompt


def test_decency_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(decency.ENV, "0")
    prompt, start = talk(tmp_path, [GUARD], 3.5, events=[react(GUARD, PLAYER, 3.4995, via="decency")])
    assert start[decency.KEY_DECENCY_ON] is False and "naked in public" not in prompt


# ---- a woman's eyes on a naked player ------------------------------------------------------------------

def test_peek_follows_attraction_mood_and_fear():
    calm = Feelings()
    stranger = peek.propensity({}, 0.0, 0.0, 0.0, 0.0, 0.0, calm, None)
    keen = peek.propensity({"bold": 0.8}, 0.5, 0.3, 0.0, 0.5, 0.6, calm, 8)
    afraid = peek.propensity({}, -0.4, -0.3, 0.6, 0.0, 0.0, calm, 2)
    shy = peek.propensity({"reserve": 0.9, "anxiety": 0.8}, 0.0, 0.0, 0.0, 0.0, 0.0, calm, None)
    assert afraid == 0.0 < shy < stranger < keen <= 0.9
    rattled = Feelings()
    rattled.add({"fear": 0.6, "irritation": 0.5})
    assert peek.propensity({}, 0.0, 0.0, 0.0, 0.0, 0.0, rattled, None) < stranger


def test_profiles_carry_peek_for_women_only(tmp_path):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, YSOLDA, actors.hod()], game_days=3.5)
        s.continue_until_player_turn()
        s.end(game_days=3.51)
    sent = [p for _, r in s.exchanges if r.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_NPCTALK
            for p in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {}).get("mantella_lw_body", [])]
    ysolda = [p for p in sent if p.startswith("Ysolda|")][0].split("|")
    hod = [p for p in sent if p.startswith("Hod|")][0].split("|")
    assert len(ysolda) == 6 and float(ysolda[5]) > 0.0
    assert float(hod[5]) == 0.0


def test_a_peek_reaches_her_next_line_and_changes_no_feelings(tmp_path):
    raw = presence(YSOLDA, "peek", 3.4998, distance_m=2.0)
    p = P.parse(raw)
    assert P.appraise(p, {}, P.Bond()) == {}
    prompt, _ = talk(tmp_path, [YSOLDA], 3.5, events=[raw])
    assert "Ysolda's eyes went down to naked Torvar's manhood for a moment" in prompt


def test_peek_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(peek.ENV, "0")
    _, start = talk(tmp_path, [YSOLDA], 3.5)
    assert start[peek.KEY_PEEK_ON] is False


# ---- fines paid on the spot, and thanes ----------------------------------------------------------------

def _whiterun_guard():
    from livingworld.guard import orgs as O
    from livingworld.integration import KEY_CRIME_FACTION, KEY_CRIME_PLUGIN, KEY_IS_GUARD, KEY_PLAYER_BOUNTY
    a = actors.actor("Whiterun Guard", 0x000A0DA4, 0x0001A6B2, 0, "Nord", "MaleGuard")
    a[KEY_IS_GUARD] = True
    a[KEY_CRIME_FACTION] = int(O.by_key("whiterun_guard").crime_faction, 16)
    a[KEY_CRIME_PLUGIN] = "Skyrim.esm"
    a[KEY_PLAYER_BOUNTY] = 0
    return a


def fine_talk(work_dir, day, reply="Hm.", thane=False):
    from livingworld.integration import KEY_THANE_OF
    guard = _whiterun_guard()
    llm = ScriptedLLM(replies=[reply])
    context = {KEY_EVENTS: [react(guard, PLAYER, day - 0.0005, via="decency")]}
    if thane:
        context[KEY_THANE_OF] = ["Whiterun"]
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, guard], location="Whiterun", game_days=day, context_extra=context)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    lines = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges
             if r.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_NPCTALK]
    return llm.dialogue_calls[0].system_prompt, lines


def test_a_repeat_fine_can_be_paid_on_the_spot_once(tmp_path):
    fine_talk(tmp_path, 3.5)
    prompt, lines = fine_talk(tmp_path, 3.8, reply="PayFine: That's twenty septims. Now cover yourself.")
    assert "If Torvar agrees to pay on the spot, use PayFine" in prompt
    paid = [l for l in lines if decency.KEY_FINE_GOLD in l]
    assert paid and paid[0][decency.KEY_FINE_GOLD] == decency.FINE and paid[0][decency.KEY_FINE_GUARD]
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = ?", (decency.EVENT_FINE,)).fetchone()[0] == 1


def test_no_fine_is_taken_for_a_first_warning(tmp_path):
    _, lines = fine_talk(tmp_path, 3.5, reply="PayFine: Twenty gold.")
    assert not any(decency.KEY_FINE_GOLD in l for l in lines)


def test_a_thane_is_asked_with_deference_and_never_fined(tmp_path):
    fine_talk(tmp_path, 3.5, thane=True)
    prompt, lines = fine_talk(tmp_path, 3.8, reply="PayFine: Twenty gold, my Thane.", thane=True)
    assert "Torvar is Thane of Whiterun" in prompt and "'my Thane'" in prompt
    assert "the Jarl could come to hear of it" in prompt and "use PayFine" not in prompt.split("Do not use")[0]
    assert not any(decency.KEY_FINE_GOLD in l for l in lines)
