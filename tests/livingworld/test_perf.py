"""The VR performance monitor: server counters, game timing markers, the timeline and the report."""
from __future__ import annotations

import json

from livingworld.perf import report as rep
from livingworld.perf.game_records import KEY_REALTIME, is_perf_record, parse_perf_record, split_perf_records
from livingworld.perf.monitor import PerfMonitor, perf_enabled, stalled
from livingworld.perf.stats import ServerStats

GRAB = ("v=1|key=9-3|type=Perf|gt=3.5|loc=Riverwood|what=Grab|hand=L|target=Gerdur|rt0=100.0|rt1=160.0"
        "|grabs=1|drops=1|dups=0|short=0|ticks=118|lag_avg=0.2|lag_max=19.8|stalls=1|grab_ms=41")
PERCEPT = "v=1|key=9-4|type=grab|gt=3.5|loc=Riverwood|actor=1^7^^^1^Me|target=2^3^^^0^Gerdur|dur=60"


def test_llm_calls_are_counted_while_waiting_and_in_flight():
    stats = ServerStats()
    with stats.llm_call("dialogue"):
        assert stats.pending_llm() == 1
        with stats.llm_call("request"):
            assert stats.pending_llm() == 2
    assert stats.pending_llm() == 0
    window = stats.take_window()
    assert window["llm_peak_pending"] == 2 and window["llm_started"] == 2 and window["llm_finished"] == 2
    assert stats.take_window()["llm_started"] == 0          # a new window starts empty


def test_llm_call_is_counted_down_even_when_it_raises():
    stats = ServerStats()
    try:
        with stats.llm_call("request"):
            raise RuntimeError("network")
    except RuntimeError:
        pass
    assert stats.pending_llm() == 0


def test_jobs_requests_and_records_in_a_window():
    stats = ServerStats()
    stats.job_started()
    stats.request_done("mantella_continue_conversation", 120.0)
    stats.request_done("mantella_player_input", 40.0)
    stats.records_ingested({"grab": 2}, 3.0)
    w = stats.take_window()
    assert w["jobs_running"] == 1 and w["jobs_peak_running"] == 1
    assert w["request_max_ms"] == 120.0 and w["request_max_type"] == "mantella_continue_conversation"
    assert w["records"] == {"grab": 2}
    stats.job_finished()
    assert stats.take_window()["jobs_done"] == 1


def test_perf_records_are_split_from_perception_and_placed_on_the_wall_clock():
    keep, markers = split_perf_records([GRAB, PERCEPT], {KEY_REALTIME: 200.0}, wall_now=10_000.0)
    assert keep == [PERCEPT]
    (m,) = markers
    assert m["what"] == "grab" and m["target"] == "Gerdur" and m["lag_max"] == 19.8
    assert m["t0"] == 9_900.0 and m["t1"] == 9_960.0


def test_perf_record_without_game_clock_has_no_wall_times():
    _, (m,) = split_perf_records([GRAB], {})
    assert "t0" not in m and m["rt0"] == 100.0


def test_only_type_perf_is_a_perf_record():
    assert is_perf_record(GRAB)
    assert not is_perf_record(PERCEPT)
    assert not is_perf_record("v=1|type=perfume|x=1")
    assert parse_perf_record("v=1|type=perf|what=stall|lag=abc")["what"] == "stall"


def test_stall_detection():
    assert stalled({"main_thread_hung": True})
    assert stalled({"main_thread_answered": False})
    assert stalled({"main_thread_answer_ms": 400})
    assert stalled({"cpu_frame_interval_ms_max": 20_000})
    assert not stalled({"main_thread_answered": True, "main_thread_answer_ms": 12, "cpu_frame_interval_ms_max": 11.2})


def test_monitor_is_off_unless_asked(monkeypatch, tmp_path):
    monkeypatch.delenv("MANTELLA_LW_PERF", raising=False)
    assert not perf_enabled()
    assert PerfMonitor.from_environment(str(tmp_path)) is None
    monkeypatch.setenv("MANTELLA_LW_PERF", "1")
    monitor = PerfMonitor.from_environment(str(tmp_path))
    assert monitor is not None and monitor.file_path.startswith(str(tmp_path))


class FakeWindow:
    def __init__(self, script):
        self.script = list(script)

    def sample(self):
        hung = self.script.pop(0)
        return {"main_thread_answered": not hung, "main_thread_answer_ms": 1000.0 if hung else 8.0}


def test_monitor_marks_a_stall_and_its_end(tmp_path):
    stats = ServerStats()
    clock = iter([0.0, 0.5, 1.0, 1.5]).__next__
    monitor = PerfMonitor(str(tmp_path), stats=stats, samplers=[FakeWindow([False, True, True, False])], clock=clock)
    stats.add_marker({"what": "grab"})
    rows = [monitor.sample_once() for _ in range(4)]
    assert rows[0]["markers"] == [{"what": "grab"}] and "markers" not in rows[1]
    assert [r.get("stall", False) for r in rows] == [False, True, True, False]
    assert rows[3]["stall_ended_s"] == 1.0


def test_a_failing_sampler_never_stops_the_timeline(tmp_path):
    class Broken:
        def sample(self):
            raise OSError("no SteamVR")
    row = PerfMonitor(str(tmp_path), stats=ServerStats(), samplers=[Broken()], clock=lambda: 5.0).sample_once()
    assert row["t"] == 5.0 and "llm_pending" in row


def _timeline(tmp_path, stall_during_hold=True, llm_busy=False):
    rows = []
    for i in range(240):                     # 120 s at 2 samples a second; hold from 40 s to 100 s
        t = 1000.0 + i * 0.5
        hung = stall_during_hold and 60.0 <= t - 1000.0 < 80.0
        rows.append({"t": t, "cpu_frame_interval_ms_avg": 20_000.0 if hung else 11.1,
                     "cpu_frame_interval_ms_max": 20_000.0 if hung else 12.0, "gpu_total_ms_avg": 7.0,
                     "gpu_total_ms_max": 8.0, "main_thread_answer_ms": 1000.0 if hung else 9.0,
                     "main_thread_answered": not hung, "stall": hung or None,
                     "llm_peak_pending": 1 if llm_busy else 0, "jobs_peak_running": 0,
                     "system_free_mb": 5000, "game_page_faults": 100})
    rows[-1]["markers"] = [{"what": "grab", "target": "Gerdur", "hand": "L", "t0": 1040.0, "t1": 1100.0,
                            "grabs": 1.0, "drops": 1.0, "lag_max": 20.0, "lag_avg": 0.3, "ticks": 100.0}]
    path = tmp_path / "perf.jsonl"
    path.write_text("\n".join(json.dumps({k: v for k, v in r.items() if v is not None}) for r in rows), encoding="utf-8")
    return str(path)


def test_report_puts_a_stall_during_the_hold_on_the_main_thread(tmp_path):
    text = rep.report(rep.load(_timeline(tmp_path)))
    assert "Hold of Gerdur" in text
    assert "## Main-thread stalls: 1" in text
    assert "20.0 s starting 60.0 s into the file during a hold of Gerdur" in text
    during = next(line for line in text.splitlines() if line.startswith("| during hold"))
    assert "main thread (engine or physics" in during
    before = next(line for line in text.splitlines() if line.startswith("| before"))
    assert before.endswith("| nothing wrong seen |")


def test_report_names_ai_as_load_only_when_it_overlaps(tmp_path):
    text = rep.report(rep.load(_timeline(tmp_path, llm_busy=True)))
    during = next(line for line in text.splitlines() if line.startswith("| during hold"))
    assert "AI processing was busy at the same time" in during


def test_classify_sources():
    assert rep.classify({"gpu_total_ms_avg": 14.0, "cpu_frame_interval_ms_avg": 14.0}, 90) == \
        ["rendering (GPU over the frame budget)"]
    assert rep.classify({"gpu_total_ms_avg": 6.0, "cpu_frame_interval_ms_avg": 22.0}, 90) == \
        ["main thread (CPU frame time over budget while the GPU has room)"]
    assert rep.classify({"gpu_total_ms_avg": 6.0, "cpu_frame_interval_ms_avg": 11.0}, 90, script_lag=2.0) == \
        ["scripting (Papyrus ticks late while frames were on time)"]
    assert rep.classify({"free_mb_min": 300}, 90)[0].startswith("memory")
    assert rep.classify({"llm_pending_max": 2}, 90) == ["AI processing busy, with no effect on frames"]
    assert rep.classify({"gpu_total_ms_avg": 6.0, "cpu_frame_interval_ms_avg": 11.0}, 90) == []


# ---- end to end through Mantella's /mantella route ----------------------------------------------

def test_game_timing_records_reach_the_monitor_and_never_the_memories(tmp_path, monkeypatch, caplog):
    from livingworld.integration import ENV_VAR, LivingWorld
    from livingworld.perf.stats import STATS
    from tests.livingworld.test_perception import grab_scene, memories, open_db, talk
    from livingworld.harness import actors
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    talk(tmp_path, [actors.gerdur()], 3.5)
    STATS.take_markers()
    STATS.take_window()
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene(), GRAB], extra=None)
    db = open_db(tmp_path)
    assert len(memories(db, "Gerdur")) == 1                          # the grab itself, once
    assert db.execute("SELECT COUNT(*) FROM world_event").fetchone()[0] == 1
    assert "perception record skipped" not in caplog.text
    (marker,) = STATS.take_markers()
    assert marker["what"] == "grab" and marker["target"] == "Gerdur"
    window = STATS.take_window()
    assert window["records"].get("grab") == 1 and window["requests"]
