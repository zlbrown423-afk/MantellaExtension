"""The duty in hand as a journal objective the game keeps on show until the shift is over (guard/hud.py)."""
import pytest

from livingworld.guard import hud
from livingworld.integration import KEY_DUTY_HUD_TASK
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import DAY, PLAYER, living_world_inline, whiterun_guard  # noqa: F401
from tests.livingworld import test_guard_duty as G
from tests.livingworld.test_guard_duty import duties, enlisted


def objectives(work_dir, day, **kw):
    """One conversation; the duty objective keys the NPC lines carried."""
    seen = []
    orig = G.HeadlessSession.end

    def end(self, *a, **k):
        seen.extend(r[comm_consts.KEY_REPLYTYPE_NPCTALK] for _, r in self.exchanges
                    if KEY_DUTY_HUD_TASK in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {}))
        return orig(self, *a, **k)

    G.HeadlessSession.end = end
    try:
        G.talk(work_dir, day, **kw)
    finally:
        G.HeadlessSession.end = orig
    return seen


def test_a_new_duty_is_sent_with_its_shift_and_whom_to_report_to(tmp_path):
    enlisted(tmp_path)
    sent = objectives(tmp_path, DAY + 0.01)
    task = duties(tmp_path)[-1]
    assert len(sent) == 1, sent                                   # once, not with every line
    o = sent[0]
    assert o[KEY_DUTY_HUD_TASK] == task["task_id"]
    assert o["mantella_lw_duty_hud_text"][:1].isupper()
    assert o["mantella_lw_duty_hud_end"] > DAY + 0.01
    assert o["mantella_lw_duty_hud_ready"] == -1.0                 # not seen at the post yet
    assert o["mantella_lw_duty_hud_report_by"] > o["mantella_lw_duty_hud_end"]
    assert o["mantella_lw_duty_hud_boss"] == "Commander Caius"
    assert o["mantella_lw_duty_hud_boss_id"] == 0x00038258 and o["mantella_lw_duty_hud_boss_plugin"] == "Skyrim.esm"
    assert o["mantella_lw_duty_hud_every"] == hud.DEFAULT_EVERY


def test_being_seen_at_the_post_says_when_it_may_be_reported(tmp_path):
    enlisted(tmp_path)
    objectives(tmp_path, DAY + 0.01)
    o = objectives(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])[-1]
    assert o["mantella_lw_duty_hud_seen"] is True
    assert DAY + 0.01 < o["mantella_lw_duty_hud_ready"] < o["mantella_lw_duty_hud_end"]


def test_reporting_back_closes_it_as_done(tmp_path):
    enlisted(tmp_path)
    objectives(tmp_path, DAY + 0.01)
    task = duties(tmp_path)[-1]
    objectives(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])
    o = objectives(tmp_path, DAY + 0.2, say="Caius, reporting back: the gate was quiet.")[0]
    assert o[KEY_DUTY_HUD_TASK] == 0
    assert o["mantella_lw_duty_hud_outcome"] == "done" and o["mantella_lw_duty_hud_last"] == task["task_id"]


def test_a_missed_duty_closes_it_as_failed(tmp_path):
    enlisted(tmp_path)
    objectives(tmp_path, DAY + 0.01)
    o = objectives(tmp_path, DAY + 1.5, npcs=[whiterun_guard()], location="Riverwood")[0]
    assert o[KEY_DUTY_HUD_TASK] == 0 and o["mantella_lw_duty_hud_outcome"] == "failed"


def test_switched_off_the_game_is_told_to_close_it(tmp_path, monkeypatch):
    enlisted(tmp_path)
    monkeypatch.setenv(hud.ENV, "0")
    o = objectives(tmp_path, DAY + 0.01)[0]
    assert o[KEY_DUTY_HUD_TASK] == 0 and o["mantella_lw_duty_hud_outcome"] == ""


def test_reminder_interval_setting(monkeypatch):
    monkeypatch.setenv(hud.ENV_EVERY, "0")
    assert hud.every_seconds() == 0.0
    monkeypatch.setenv(hud.ENV_EVERY, "junk")
    assert hud.every_seconds() == hud.DEFAULT_EVERY
    monkeypatch.setenv(hud.ENV_EVERY, "-5")
    assert hud.every_seconds() == 0.0


def test_not_in_a_guard_sends_nothing(tmp_path):
    assert objectives(tmp_path, DAY) == []


def test_a_camp_raid_marks_the_camp_until_the_fight_is_on_record(tmp_path):
    """User 2026-11-17: a compass marker on the bandit camp, and Valtheim Towers as the next one."""
    enlisted(tmp_path)
    G.talk(tmp_path, DAY + 0.005, say="debug guard incident camp valtheim", npcs=[whiterun_guard()],
           location="Riverwood")
    sent = objectives(tmp_path, DAY + 0.01)
    task = duties(tmp_path)[-1]
    assert task["template"].startswith("duty:incident:camp_raid:") and task["location"] == "Valtheim Towers"
    assert sent[-1]["mantella_lw_duty_hud_target_id"] == 0x000162C9
    assert sent[-1]["mantella_lw_duty_hud_target_plugin"] == "Skyrim.esm"
    sent = objectives(tmp_path, DAY + 0.3, npcs=[whiterun_guard()], location="Whiterun",
                      events=[G.fight(PLAYER, 0.5, DAY + 0.25, location="Valtheim Towers")])
    assert sent and sent[-1]["mantella_lw_duty_hud_target_id"] == 0          # dealt with: back to the superior only


def test_the_camp_marker_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(hud.ENV_MARKER, "0")
    enlisted(tmp_path)
    G.talk(tmp_path, DAY + 0.005, say="debug guard incident camp", npcs=[whiterun_guard()], location="Riverwood")
    sent = objectives(tmp_path, DAY + 0.01)
    assert sent[-1]["mantella_lw_duty_hud_target_id"] == 0


def test_routine_duties_have_no_place_marker(tmp_path):
    enlisted(tmp_path)
    sent = objectives(tmp_path, DAY + 0.01)
    assert sent[-1]["mantella_lw_duty_hud_target_id"] == 0
