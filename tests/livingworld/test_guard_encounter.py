"""G2e shift encounters: watch windows on a guard shift, people who come over, things noticed, and the
shift summary in the report."""
from livingworld.guard import encounter as ENC, orgs as O
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, watch
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import DAY, PLAYER, caius, living_world_inline, npc_id, open_db, service, whiterun_guard  # noqa: F401
from tests.livingworld.test_guard_duty import duties, enlisted, given_duty, talk

WHITERUN = O.by_key("whiterun_guard")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BAC, 1, "Nord", "FemaleYoungEager")


def events(tmp_path, kind):
    return [dict(r) for r in open_db(tmp_path).execute(
        "SELECT * FROM world_event WHERE type = ? ORDER BY event_id", (f"guard.{kind}",))]


def test_a_shift_with_nothing_at_all_is_about_one_in_a_hundred():
    plans = [ENC.windows(WHITERUN, task_id, 10.3, 4) for task_id in range(1, 20001)]
    quiet = sum(1 for p in plans if not p) / len(plans)
    assert 0.006 < quiet < 0.015, quiet
    mean = sum(len(p) for p in plans) / len(plans)
    assert 1.2 < mean < 2.0, mean                         # a few small matters, not a crowd
    spread = {w.win for p in plans for w in p}
    assert spread == {0, 1, 2}                            # beginning, middle and end of the shift
    some = next(p for p in plans if len(p) == 3)
    assert 10.3 < some[0].at < some[1].at < some[2].at < 10.3 + 4 / 24


def test_the_game_is_told_the_watch_windows_of_a_new_duty(tmp_path):
    enlisted(tmp_path)
    prompt = talk(tmp_path, DAY + 0.01)
    assert "people may come to Torvar with questions" in prompt
    task = duties(tmp_path)[-1]
    keys = talk.watch[-1]
    assert keys["mantella_lw_guard_watch_task"] == task["task_id"]
    assert len(keys["mantella_lw_guard_watch_at"]) == len(keys["mantella_lw_guard_watch_win"]) \
        == len(keys["mantella_lw_guard_watch_kind"]) == len(keys["mantella_lw_guard_watch_obs"])
    assert keys["mantella_lw_guard_watch_cooldown"] == 3


def test_someone_comes_over_with_a_reason_and_answering_counts(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)["task_id"]
    rec = watch(PLAYER, task, 1, DAY + 0.06, target_json=YSOLDA, location="Whiterun")
    prompt = talk(tmp_path, DAY + 0.06, say="What can I do for you?", npcs=[YSOLDA], events=[rec])
    assert "Ysolda has come over to Torvar, a Recruit of the Whiterun Guard on duty, because" in prompt
    enc = events(tmp_path, "encounter")
    assert len(enc) == 1 and enc[0]["location"] == "Whiterun"
    assert len(events(tmp_path, "encounter_answered")) == 1
    # the game sends the same record twice (an older save): stored once
    talk(tmp_path, DAY + 0.07, npcs=[YSOLDA], events=[rec])
    assert len(events(tmp_path, "encounter")) == 1


def test_nobody_near_means_noticing_something_and_it_goes_in_the_report(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)["task_id"]
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()], events=[watch(PLAYER, task, 0, DAY + 0.03)])
    obs = events(tmp_path, "observation")
    assert len(obs) == 1
    talk(tmp_path, DAY + 0.06, say="Hello there.", npcs=[YSOLDA],
         events=[watch(PLAYER, task, 1, DAY + 0.06, target_json=YSOLDA)])
    merit = service(open_db(tmp_path))["merit"]
    prompt = talk(tmp_path, DAY + 0.2)                                   # back to Caius: report filed by itself
    assert duties(tmp_path)[-1]["state"] == "completed"
    assert "What Torvar has to report from the shift" in prompt and "people who came to them: Ysolda" in prompt
    assert "what they noticed:" in prompt
    summary = events(tmp_path, "shift_summary")
    assert len(summary) == 1
    assert any("Shift: 1 encounter, 1 observation" in h for h in talk.hints)
    assert service(open_db(tmp_path))["merit"] > merit + 1.0              # the duty plus the person dealt with
    assert "1 person dealt with on shift" in talk(tmp_path, DAY + 0.21)


def test_a_fellow_guard_brings_word_and_the_superior_inspects(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)["task_id"]
    prompt = talk(tmp_path, DAY + 0.04, npcs=[whiterun_guard()],
                  events=[watch(PLAYER, task, 0, DAY + 0.04, target_json=whiterun_guard())])
    assert "a fellow member of the Whiterun Guard, has come over to Torvar" in prompt
    assert '"category": "military"' in events(tmp_path, "encounter_reason")[-1]["payload_json"]
    prompt = talk(tmp_path, DAY + 0.05, npcs=[caius()],
                  events=[watch(PLAYER, task, 1, DAY + 0.05, target_json=caius())])
    assert "has come over to Torvar, their subordinate (Recruit)" in prompt
    assert '"category": "inspection"' in events(tmp_path, "encounter_reason")[-1]["payload_json"]


def test_a_child_never_comes_over(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)["task_id"]
    child = actors.actor("Lars Battle-Born", 0x00013BAF, 0x0001A68C, 0, "Nord", "MaleChild")
    talk(tmp_path, DAY + 0.04, npcs=[whiterun_guard()], events=[watch(PLAYER, task, 0, DAY + 0.04, target_json=child)])
    assert events(tmp_path, "encounter") == [] and len(events(tmp_path, "observation")) == 1


def test_switching_encounters_off(tmp_path, monkeypatch):
    monkeypatch.setenv(ENC.ENV, "0")
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)["task_id"]
    assert talk.watch == []
    talk(tmp_path, DAY + 0.04, npcs=[YSOLDA], events=[watch(PLAYER, task, 0, DAY + 0.04, target_json=YSOLDA)])
    assert events(tmp_path, "encounter") == []


def test_debug_watch_brings_the_windows_forward(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.02, say="debug guard watch", npcs=[whiterun_guard()])
    keys = talk.watch[-1]                    # sent again with the reply that follows the command
    assert keys["mantella_lw_guard_watch_kind"] == [1, 1, 0]
    assert all(DAY + 0.02 < t < DAY + 0.04 for t in keys["mantella_lw_guard_watch_at"])


def test_a_footlocker_for_the_bunk_is_sent_once_per_service(tmp_path):
    assert chests(tmp_path, DAY - 0.01) == []                       # not serving: nothing
    enlisted(tmp_path)                                              # sent with the bunk on joining
    assert len(events(tmp_path, "chest_granted")) == 1
    assert chests(tmp_path, DAY + 0.01) + chests(tmp_path, DAY + 0.02) == []
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.bed_granted'").fetchone()[0] == 1


def chests(tmp_path, day):
    from livingworld.harness.scripted_llm import ScriptedLLM
    from livingworld.harness.session import HeadlessSession
    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Hm."])) as s:
        s.start([PLAYER, whiterun_guard()], location="Whiterun", game_days=day)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
        lines = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
    return [(x["mantella_lw_guard_chest_id"], x["mantella_lw_guard_chest_plugin"]) for x in lines
            if "mantella_lw_guard_chest_id" in x]
