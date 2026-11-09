"""G2 "Duties, reports, record, pay and probation" for the Whiterun Guard.

A recruit who talks to Commander Caius (their superior) is given a duty; being seen at the post in another
conversation during the shift and then reporting back after half the shift counts as done; reporting
without being seen is a false report; never reporting is a missed duty. Every reprimand docks pay and can
extend probation; five honest duties in a row earn a commendation. All of it rolls back with saves.
"""
import pytest

from livingworld.guard import duty as DU, orgs as O, store as S
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import KEY_GUARD_ISSUE, LivingWorld
from livingworld.store.world_store import WorldStore
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import (DAY, ENLIST, PLAYER, caius, db_path, living_world_inline, load,  # noqa: F401
                                          npc_id, open_db, run_dev, service, whiterun_guard)

WHITERUN = O.by_key("whiterun_guard")
REPORT = when_player_says("reporting back", "ReportDuty: Let's hear it, then.")
PAY = when_player_says("my wages", "PayWages: Here, your pay.")


def talk(work_dir, day, say=None, npcs=None, location="Whiterun", extra=None):
    llm = ScriptedLLM(rules=[ENLIST, REPORT, PAY], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *(npcs or [caius()])], location=location, game_days=day, extra=extra)
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=day + 0.001)
        LivingWorld.latest.wait_for_jobs()
        talk.reply = s.exchanges[1][1]
        lines = [r[comm_consts.KEY_REPLYTYPE_NPCTALK] for _, r in s.exchanges
                 if KEY_GUARD_ISSUE in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})]
        talk.issued = [line[KEY_GUARD_ISSUE] for line in lines]
        talk.counts = [line.get("mantella_lw_guard_issue_counts") for line in lines]
        talk.hints = [h for _, r in s.exchanges for h in ((r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})
                                                           .get("mantella_lw_hints") or [])]
    return llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""


def duties(work_dir):
    db = open_db(work_dir)
    return [dict(r) for r in db.execute("SELECT * FROM v_task WHERE template LIKE 'duty:%' AND is_reset = 0"
                                        " ORDER BY task_id")]


def enlisted(work_dir, day=DAY):
    talk(work_dir, day, say="I want to join the guard.")
    assert service(open_db(work_dir))["status"] == "probation"


def given_duty(work_dir, day):
    prompt = talk(work_dir, day)
    assert "has a duty for Torvar right now" in prompt, prompt
    return duties(work_dir)[-1]


# ---- giving a duty ----

def test_caius_gives_his_recruit_a_duty_that_fits_the_hour_and_rank(tmp_path):
    enlisted(tmp_path)
    prompt = talk(tmp_path, DAY + 0.01)
    task = duties(tmp_path)[-1]
    assert task["state"] == "accepted" and task["assignee_id"] == npc_id(open_db(tmp_path), "Torvar")
    assert task["giver_id"] == npc_id(open_db(tmp_path), "Commander Caius")
    d = DU.rules("whiterun_guard").duty(task["template"])
    assert d.key in ("gate_watch", "wall_patrol", "market_watch", "barracks_drill")   # 9 am, recruit: no night watch
    assert f"has a duty for Torvar right now: {d.title}" in prompt
    assert "begin the reply with 'ReportDuty:'" in prompt
    assert "a bunk in the Whiterun barracks" in prompt
    # the next conversation reminds rather than giving another
    prompt = talk(tmp_path, DAY + 0.02)
    assert len(duties(tmp_path)) == 1 and f"gave Torvar a duty: {d.title}" in prompt


def test_other_guards_know_the_player_is_on_duty_but_give_no_orders(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()], location="Riverwood")
    assert f"Torvar is on duty right now: they were told to {task['title']}" in prompt
    assert "has a duty for Torvar" not in prompt and len(duties(tmp_path)) == 1


# ---- reporting back ----

def test_an_honest_report_after_being_seen_at_the_post(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])             # seen at the post (Whiterun) by a guard
    talk(tmp_path, DAY + 0.2, say="Caius, reporting back: the gate was quiet.")
    task = duties(tmp_path)[-1]
    assert task["state"] == "completed"
    db = open_db(tmp_path)
    assert service(db)["merit"] > 0
    mem = db.execute("SELECT content FROM memory WHERE owner_id = ? AND content LIKE '%reported back%'",
                     (npc_id(db, "Commander Caius"),)).fetchone()
    assert mem and "the gate was quiet" in mem[0]


def test_reporting_without_being_seen_is_a_false_report(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()], location="Riverwood")   # off at Riverwood instead
    talk(tmp_path, DAY + 0.2, say="Reporting back, all quiet.")                   # Caius in Whiterun: not a sighting
    assert duties(tmp_path)[-1]["state"] == "failed"
    db = open_db(tmp_path)
    assert service(db)["merit"] == pytest.approx(-3.0)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.reprimanded'").fetchone()[0] == 1
    prompt = talk(tmp_path, DAY + 0.6)
    assert "1 false report" in prompt and "1 reprimand" in prompt


def test_coming_back_early_counts_as_leaving_the_post(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    talk(tmp_path, DAY + 0.03, say="Reporting back.")
    task = duties(tmp_path)[-1]
    assert task["state"] == "failed"
    assert service(open_db(tmp_path))["merit"] == pytest.approx(-1.0)


def test_never_reporting_is_a_missed_duty(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 1.5, npcs=[whiterun_guard()], location="Riverwood")   # long after shift + grace
    db = open_db(tmp_path)
    assert duties(tmp_path)[-1]["state"] == "expired"
    assert service(db)["merit"] == pytest.approx(-2.0)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.reprimanded'").fetchone()[0] == 1
    # word gets around
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'reprimanded'").fetchone()[0] >= 1


# ---- rules exercised directly on the store ----

def with_db(work_dir, fn):
    store = WorldStore(db_path(work_dir))
    try:
        with store.transaction() as db:
            return fn(db)
    finally:
        store.close()


def test_five_honest_duties_earn_a_commendation(tmp_path):
    enlisted(tmp_path)

    def run(db):
        me, boss = npc_id(db, "Torvar"), npc_id(db, "Commander Caius")
        results = []
        for i in range(5):
            svc = S.current(db, me)
            day = DAY + 1 + i
            d = DU.pick(WHITERUN, svc, None, day, any_hour=True)
            DU.assign(db, WHITERUN, svc, boss, d, day)
            DU.mark_seen(db, me, "Whiterun", day + 0.01)
            results.append(DU.report(db, WHITERUN, S.current(db, me), boss, day + 0.3, "done"))
        return results, DU.record(db, S.current(db, me))

    results, rec = with_db(tmp_path, run)
    assert [r.outcome for r in results] == ["done"] * 5
    assert [r.commended for r in results] == [False] * 4 + [True]
    assert rec.done == 5 and rec.commended == 1


def test_probation_is_confirmed_extended_or_ends_in_dismissal(tmp_path):
    enlisted(tmp_path)

    def clean(db):
        me = npc_id(db, "Torvar")
        assert DU.upkeep(db, DAY + 8)["confirmed"]
        return S.current(db, me).status

    assert with_db(tmp_path, clean) == "active"


def test_a_reprimand_extends_probation_and_a_second_dismisses(tmp_path):
    enlisted(tmp_path)

    def run(db):
        me, boss = npc_id(db, "Torvar"), npc_id(db, "Commander Caius")
        DU.reprimand(db, S.current(db, me), boss, "asleep at the gate", -1, DAY + 1)
        assert DU.upkeep(db, DAY + 7.5)["extended"]
        assert S.current(db, me).status == "probation"
        DU.reprimand(db, S.current(db, me), boss, "drunk on watch", -1, DAY + 9)
        assert DU.upkeep(db, DAY + 15)["dismissed"]
        return S.current(db, me)

    assert with_db(tmp_path, run) is None


def test_wages_are_owed_weekly_docked_for_reprimands_and_paid_in_gold(tmp_path):
    enlisted(tmp_path)

    def setup(db):
        me, boss = npc_id(db, "Torvar"), npc_id(db, "Commander Caius")
        DU.reprimand(db, S.current(db, me), boss, "late for drill", -1, DAY + 1)
        return DU.pay(db, WHITERUN, S.current(db, me), DAY + 14.5)

    p = with_db(tmp_path, setup)
    assert (p.weeks, p.earned, p.docked, p.paid, p.owed) == (2, 120, 25, 0, 95)
    prompt = talk(tmp_path, DAY + 14.5, say="Commander, I'd like my wages.")
    assert "owes Torvar 95 gold" in prompt and "'PayWages:'" in prompt
    assert talk.issued == ["Gold|Skyrim.esm|0x0000000F|95"] and talk.counts == [[95]]
    prompt = talk(tmp_path, DAY + 14.6, say="Commander, I'd like my wages again.")
    assert "Nothing more is owed right now" in prompt and talk.issued == []


def test_caius_wants_a_word_when_a_duty_is_waiting(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.01, npcs=[whiterun_guard()], location="Riverwood")
    assert "Commander Caius" in (talk.reply.get("mantella_lw_wants_a_word") or "")


# ---- saves, test commands, developer tool ----

def test_loading_an_older_save_rolls_the_duty_back(tmp_path):
    talk(tmp_path, DAY, extra=load(0, 301, DAY - 0.1))
    timeline = talk.reply["mantella_lw_timeline_id"]
    talk(tmp_path, DAY + 0.01, say="I want to join the guard.", extra=load(timeline, 301, DAY - 0.1))
    talk(tmp_path, DAY + 0.02, extra=load(timeline, 301, DAY - 0.1))
    assert len(duties(tmp_path)) == 1
    talk(tmp_path, DAY + 0.016, npcs=[whiterun_guard()], location="Riverwood",
         extra=load(timeline, 302, DAY + 0.015))                              # saved before the duty
    assert [d for d in duties(tmp_path) if d["assignee_id"] is not None] == []


def test_in_game_duty_commands(tmp_path):
    talk(tmp_path, DAY, say="debug guard enlist")
    talk(tmp_path, DAY + 0.01, say="debug guard duty")
    assert duties(tmp_path)[-1]["state"] in ("accepted", "in_progress")
    talk(tmp_path, DAY + 0.02, say="debug guard seen")
    assert duties(tmp_path)[-1]["state"] == "in_progress"
    talk(tmp_path, DAY + 0.4, say="debug guard report")
    assert duties(tmp_path)[-1]["state"] == "completed"


def test_developer_duty_tools(tmp_path, monkeypatch):
    from livingworld.guard import dev
    monkeypatch.setattr(dev, "server_running", lambda: False)
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.01, npcs=[whiterun_guard()], location="Riverwood")
    assert run_dev(tmp_path, "duty")[1][-1].startswith(("duty ", "already on duty"))
    assert run_dev(tmp_path, "seen")[1][-1].startswith("seen at the post")
    assert run_dev(tmp_path, "--now", str(DAY + 0.4), "report")[1][-1] == "report: done"
    assert run_dev(tmp_path, "record")[1][-1].startswith("record: 1 duty done")
    assert run_dev(tmp_path, "--now", str(DAY + 7.1), "pay")[1][-1].startswith("pay: 1 weeks, earned 60")


def irileth():
    from tests.livingworld.test_guard import guard_keys
    from livingworld.harness import actors
    return guard_keys(actors.actor("Irileth", 0x00013BB8, 0x00013BB9, 1, "DarkElf", "FemaleDarkElf"), is_guard=False)


def test_irileth_sends_her_recruit_to_caius_for_duties(tmp_path):
    prompt = talk(tmp_path, DAY, say="I want to join the guard.", npcs=[irileth()])
    assert "A recruit takes duties and wages from Commander Caius, not from Irileth." in prompt
    assert service(open_db(tmp_path))["status"] == "probation"
    prompt = talk(tmp_path, DAY + 0.01, npcs=[irileth()])
    assert "duties, reports and wages go through Commander Caius" in prompt
    assert "has a duty for Torvar" not in prompt and duties(tmp_path) == []


def test_on_screen_notes_when_a_shift_starts_and_ends(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.01)
    assert any(h.startswith("Duty: ") and "report back to Commander Caius" in h for h in talk.hints), talk.hints
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])
    assert "On duty: seen at your post (Whiterun)." in talk.hints
    talk(tmp_path, DAY + 0.3, npcs=[whiterun_guard()], location="Riverwood")
    assert "Shift over: report back to Commander Caius." in talk.hints


def test_the_prompt_gives_clock_times_for_the_shift():
    from livingworld.guard import prompts as P
    assert P.clock(3 + 9 / 24 + 40 / 1440) == "9:40 am" and P.clock(3 + 13 / 24 + 40 / 1440) == "1:40 pm"
    assert P.clock(4.0) == "12:00 am"


def test_the_prompt_says_times_in_words_so_mantella_does_not_split_on_the_colon():
    from livingworld.guard import prompts as P
    assert P.spoken_time(3 + 21 / 24 + 41 / 1440) == "a quarter to ten in the evening"
    assert P.spoken_time(3 + 6 / 24) == "six in the morning"
    assert P.spoken_time(3 + 13 / 24 + 30 / 1440) == "half past one in the afternoon"
    assert P.spoken_time(4.0) == "midnight" and P.spoken_time(3.5) == "noon"
    assert P.spoken_time(3 + 23 / 24 + 55 / 1440) == "midnight"
    assert P.spoken_time(3 + 23 / 24 + 45 / 1440) == "a quarter to midnight"


def test_caius_reminds_with_the_real_shift_times(tmp_path):
    enlisted(tmp_path)
    task = given_duty(tmp_path, DAY + 0.01)
    d = DU.rules("whiterun_guard").duty(task["template"])
    prompt = talk(tmp_path, DAY + 0.01 + d.shift_hours / 24)          # exactly at the end of the shift
    assert "the shift runs from" in prompt and "so it is over." in prompt


def test_no_duty_at_night_is_said_plainly_and_not_invented(tmp_path):
    enlisted(tmp_path, day=3.05)                          # 1:12 am: nothing for a recruit until the 6 am drill
    prompt = talk(tmp_path, 3.06)
    assert "has no duty for Torvar right now (no duty for a Recruit at this hour)" in prompt
    assert "starts at six in the morning" in prompt and "does not invent a duty" in prompt
    assert duties(tmp_path) == []


def test_a_recruit_gets_the_evening_lamp_round(tmp_path):
    enlisted(tmp_path, day=3.86)                          # 8:38 pm, the hour of the user's first try
    prompt = talk(tmp_path, 3.87)
    assert "walk the evening lamp round" in prompt and duties(tmp_path)[-1]["template"] == "duty:lamp_round"


def test_resting_between_duties_says_when_to_come_back(tmp_path):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])
    talk(tmp_path, DAY + 0.2, say="Reporting back.")
    prompt = talk(tmp_path, DAY + 0.21)
    assert "has no duty for Torvar right now (last duty ended" in prompt and "starts at" in prompt
