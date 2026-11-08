"""Read a performance timeline and say where the time went.

    python -m livingworld.perf.report <perf-....jsonl> [--hz 90] [--margin 30]

For every NPC hold the game reported (``what=grab`` markers) it compares the ``--margin`` seconds before
the grab, the hold itself and the ``--margin`` seconds after the release. For every stall (the game's main
thread not answering) it says how long it lasted, whether it overlapped a hold, and what the server, the
GPU and memory were doing. Then it names the likely source: rendering, CPU/main thread (engine, physics),
scripting, AI processing, memory, or a mix.

What each source looks like here:

* rendering: GPU time per frame over the frame budget while the main thread keeps answering.
* main thread (engine/physics): the game stops answering its window, or the CPU frame interval is far
  over budget while GPU time is low. Havok physics, HIGGS and PLANCK run on this thread. Papyrus scripts
  run in their own time slice and cannot stop it on their own.
* scripting: Papyrus ticks run late while frames are on time.
* AI processing: LLM calls or background jobs busy. They run in the server, a separate process, so they
  can only add load (CPU, memory) to the machine; they cannot hold the game's main thread.
* memory: little free memory or a burst of page faults at the time.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

LOW_FREE_MB = 700
PAGE_FAULT_BURST = 20000     # page faults in one half-second sample


def load(path: str) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def markers(rows: list[dict[str, Any]], what: str) -> list[dict[str, Any]]:
    return [m for r in rows for m in r.get("markers", []) if m.get("what") == what]


@dataclass
class Phase:
    name: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    def values(self, key: str) -> list[float]:
        return [r[key] for r in self.rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]

    def avg(self, key: str) -> float | None:
        v = self.values(key)
        return sum(v) / len(v) if v else None

    def peak(self, key: str) -> float | None:
        v = self.values(key)
        return max(v) if v else None

    def low(self, key: str) -> float | None:
        v = self.values(key)
        return min(v) if v else None

    def total(self, key: str) -> float:
        return sum(self.values(key))

    def stall_seconds(self, interval: float) -> float:
        return sum(1 for r in self.rows if r.get("stall")) * interval

    def records(self, kind: str) -> int:
        return sum(r.get("records", {}).get(kind, 0) for r in self.rows)


def _f(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def summarize(phase: Phase, interval: float) -> dict[str, Any]:
    return {
        "samples": len(phase.rows),
        "cpu_frame_interval_ms_avg": phase.avg("cpu_frame_interval_ms_avg"),
        "cpu_frame_interval_ms_max": phase.peak("cpu_frame_interval_ms_max"),
        "gpu_total_ms_avg": phase.avg("gpu_total_ms_avg"),
        "gpu_total_ms_max": phase.peak("gpu_total_ms_max"),
        "reprojected": phase.total("reprojected_frames"),
        "dropped": phase.total("dropped"),
        "main_thread_answer_ms_max": phase.peak("main_thread_answer_ms"),
        "stall_s": phase.stall_seconds(interval),
        "llm_pending_max": phase.peak("llm_peak_pending"),
        "jobs_running_max": phase.peak("jobs_peak_running"),
        "request_ms_max": phase.peak("request_max_ms"),
        "grab_records": phase.records("grab"),
        "free_mb_min": phase.low("system_free_mb"),
        "page_faults_max": phase.peak("game_page_faults"),
        "game_cpu_avg": phase.avg("game_cpu_percent"),
    }


def classify(s: dict[str, Any], hz: float, script_lag: float | None = None) -> list[str]:
    """The likely sources of trouble in one phase, most certain first. Empty: nothing wrong seen."""
    budget = 1000.0 / hz
    found = []
    if (s.get("free_mb_min") is not None and s["free_mb_min"] < LOW_FREE_MB) or \
            (s.get("page_faults_max") or 0) >= PAGE_FAULT_BURST:
        found.append("memory (little free memory or heavy paging)")
    if s.get("stall_s"):
        found.append("main thread (engine or physics: the game stopped answering)")
    else:
        cpu, gpu = s.get("cpu_frame_interval_ms_avg"), s.get("gpu_total_ms_avg")
        if gpu is not None and gpu > budget:
            found.append("rendering (GPU over the frame budget)")
        if cpu is not None and cpu > budget * 1.2 and (gpu is None or gpu <= budget):
            found.append("main thread (CPU frame time over budget while the GPU has room)")
    if script_lag is not None and script_lag > 0.25 and not s.get("stall_s"):
        found.append("scripting (Papyrus ticks late while frames were on time)")
    if (s.get("llm_pending_max") or 0) > 0 or (s.get("jobs_running_max") or 0) > 0:
        if found:
            found.append("AI processing was busy at the same time (separate process: load only, cannot hold the game)")
        else:
            found.append("AI processing busy, with no effect on frames")
    return found


def phases_for(rows: list[dict[str, Any]], t0: float, t1: float, margin: float) -> list[Phase]:
    before, during, after = Phase("before"), Phase("during hold"), Phase("after")
    for r in rows:
        t = r.get("t", 0)
        if t0 - margin <= t < t0:
            before.rows.append(r)
        elif t0 <= t <= t1:
            during.rows.append(r)
        elif t1 < t <= t1 + margin:
            after.rows.append(r)
    return [before, during, after]


def stalls(rows: list[dict[str, Any]], interval: float) -> list[tuple[float, float, list[dict[str, Any]]]]:
    out, current = [], []
    for r in rows:
        if r.get("stall"):
            current.append(r)
        elif current:
            out.append((current[0]["t"], r.get("t", current[-1]["t"] + interval), current))
            current = []
    if current:
        out.append((current[0]["t"], current[-1]["t"] + interval, current))
    return out


def report(rows: list[dict[str, Any]], hz: float = 90.0, margin: float = 30.0) -> str:
    if not rows:
        return "No samples in this file."
    interval = 0.5
    if len(rows) > 1:
        interval = max(0.1, (rows[-1]["t"] - rows[0]["t"]) / (len(rows) - 1))
    lines = [f"# Performance timeline: {len(rows)} samples, {rows[-1]['t'] - rows[0]['t']:.0f} s, "
             f"frame budget {1000 / hz:.1f} ms ({hz:g} Hz)", ""]
    have_vr = any("cpu_frame_interval_ms_avg" in r for r in rows)
    have_window = any("main_thread_answer_ms" in r for r in rows)
    if not have_vr:
        lines.append("No SteamVR frame timings (install `openvr` in the server's Python and keep SteamVR running).")
    if not have_window:
        lines.append("No main-thread probe (the game window was not found, or this is not Windows).")

    holds = [m for m in markers(rows, "grab") if "t0" in m and "t1" in m]
    for m in holds:
        lines += ["", f"## Hold of {m.get('target', '?')} ({m.get('hand', '?')} hand), "
                      f"{m['t1'] - m['t0']:.0f} s",
                  f"Game side: {int(m.get('grabs', 0))} grab and {int(m.get('drops', 0))} drop events, "
                  f"{int(m.get('dups', 0))} repeats ignored; script ticks {int(m.get('ticks', 0))}, "
                  f"latest {_f(m.get('lag_max'), 2)} s, average {_f(m.get('lag_avg'), 3)} s, "
                  f"{int(m.get('stalls', 0))} over 1 s; grab handler {_f(m.get('grab_ms'), 0)} ms.",
                  "", "| | CPU interval avg/max ms | GPU avg/max ms | reprojected | stall s | main thread max ms "
                  "| LLM pending | jobs | grab records | free MB | page faults max | likely source |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for phase in phases_for(rows, m["t0"], m["t1"], margin):
            s = summarize(phase, interval)
            lag = m.get("lag_max") if phase.name == "during hold" else None
            src = classify(s, hz, lag) if s["samples"] else ["no samples"]
            lines.append(
                f"| {phase.name} | {_f(s['cpu_frame_interval_ms_avg'])}/{_f(s['cpu_frame_interval_ms_max'])} "
                f"| {_f(s['gpu_total_ms_avg'])}/{_f(s['gpu_total_ms_max'])} | {int(s['reprojected'])} "
                f"| {s['stall_s']:.1f} | {_f(s['main_thread_answer_ms_max'], 0)} | {_f(s['llm_pending_max'], 0)} "
                f"| {_f(s['jobs_running_max'], 0)} | {s['grab_records']} | {_f(s['free_mb_min'], 0)} "
                f"| {_f(s['page_faults_max'], 0)} | {'; '.join(src) or 'nothing wrong seen'} |")
    if not holds:
        lines += ["", "No NPC hold markers with times in this file. The game sends them with its next Mantella "
                      "request, so start a conversation after the test hold."]

    episodes = stalls(rows, interval)
    lines += ["", f"## Main-thread stalls: {len(episodes)}"]
    for start, end, eps in episodes:
        overlapping = [m for m in holds if m["t0"] - 1 <= start <= m["t1"] + 1]
        s = summarize(Phase("stall", eps), interval)
        lines.append(f"- {end - start:.1f} s starting {start - rows[0]['t']:.1f} s into the file"
                     f"{' during a hold of ' + overlapping[0].get('target', '?') if overlapping else ''}: "
                     f"LLM pending {_f(s['llm_pending_max'], 0)}, jobs {_f(s['jobs_running_max'], 0)}, "
                     f"free memory {_f(s['free_mb_min'], 0)} MB, page faults {_f(s['page_faults_max'], 0)}; "
                     f"likely {'; '.join(classify(s, hz)) or 'main thread'}.")
    late = markers(rows, "stall")
    if late:
        lines += ["", f"Late script ticks reported by the game: {len(late)} (longest "
                      f"{max(m.get('lag', 0) for m in late):.1f} s, {sum(1 for m in late if m.get('held'))} while holding someone)."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file")
    parser.add_argument("--hz", type=float, default=90.0, help="headset refresh rate (default 90)")
    parser.add_argument("--margin", type=float, default=30.0, help="seconds before and after each hold (default 30)")
    args = parser.parse_args(argv)
    print(report(load(args.file), args.hz, args.margin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
