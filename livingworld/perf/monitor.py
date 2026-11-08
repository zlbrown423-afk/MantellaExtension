"""The VR performance monitor: one background thread in the Mantella server that writes a timeline.

Switched on with ``MANTELLA_LW_PERF=1``. Every half second it writes one JSON line to
``<save folder>/data/livingworld/perf/perf-<date>-<time>.jsonl`` with:

* SteamVR frame timings (CPU frame interval, GPU time, reprojected and dropped frames) if the ``openvr``
  package is installed and SteamVR is running,
* whether the game's main thread answers its window, and how fast,
* the game's memory, page faults and CPU, and free memory (if ``psutil`` is installed),
* the server's own work: LLM calls waiting or in flight, background jobs running, game requests and how
  long the slowest took, perception records received by type,
* any timing markers the game sent (each NPC hold, each late script tick; see ``game_records.py``).

It only watches. It runs outside the game process, so it cannot slow the game's frames, and it never
changes what the server does. When the game's main thread stops answering it logs one warning when the
stall starts and one when it ends, with what the server was doing at the time.

Read a timeline with ``python -m livingworld.perf.report <file.jsonl>``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

from livingworld.perf.samplers import GameWindowSampler, OpenVRSampler, ProcessSampler
from livingworld.perf.stats import STATS, ServerStats

logger = logging.getLogger("Mantella")

ENV_VAR = "MANTELLA_LW_PERF"
INTERVAL_ENV = "MANTELLA_LW_PERF_INTERVAL"
STALL_MS = 250.0           # the main thread not answering for this long counts as a stall
_ON = {"1", "true", "yes", "on"}


def perf_enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in _ON


def stalled(sample: dict[str, Any]) -> bool:
    if sample.get("main_thread_hung") or sample.get("main_thread_answered") is False:
        return True
    if sample.get("main_thread_answer_ms", 0) >= STALL_MS:
        return True
    return sample.get("cpu_frame_interval_ms_max", 0) >= STALL_MS


class PerfMonitor:
    def __init__(self, folder: str, interval: float = 0.5, stats: ServerStats = STATS,
                 samplers: list[Any] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.folder = folder
        self.interval = interval
        self.stats = stats
        self.samplers = samplers if samplers is not None else [OpenVRSampler(), GameWindowSampler(), ProcessSampler()]
        self.clock = clock
        self.file_path = os.path.join(folder, f"perf-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stall_since: float | None = None
        self._stall_peak: dict[str, Any] = {}

    @classmethod
    def from_environment(cls, save_folder: str) -> "PerfMonitor | None":
        if not perf_enabled():
            return None
        try:
            interval = max(0.1, float(os.environ.get(INTERVAL_ENV, "0.5")))
        except ValueError:
            interval = 0.5
        monitor = cls(os.path.join(save_folder, "data", "livingworld", "perf"), interval)
        missing = [name for name, s in (("openvr", monitor.samplers[0]), ("psutil", monitor.samplers[2]))
                   if not getattr(s, "available", True)]
        logger.log(23, f"Living World performance monitor is on ({ENV_VAR}); writing {monitor.file_path}"
                   + (f". Not installed, so not measured: {', '.join(missing)}" if missing else ""))
        return monitor

    def start(self) -> None:
        os.makedirs(self.folder, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="livingworld-perf", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for s in self.samplers:
            close = getattr(s, "close", None)
            if close:
                close()

    def sample_once(self) -> dict[str, Any]:
        row: dict[str, Any] = {"t": round(self.clock(), 3)}
        for sampler in self.samplers:
            try:
                row.update(sampler.sample())
            except Exception as e:
                logger.debug(f"Living World performance monitor: {type(sampler).__name__} failed ({e})")
        row.update(self.stats.take_window())
        markers = self.stats.take_markers()
        if markers:
            row["markers"] = markers
        self._watch_stall(row)
        return row

    def _watch_stall(self, row: dict[str, Any]) -> None:
        now = row["t"]
        if stalled(row):
            if self._stall_since is None:
                self._stall_since = now
                self._stall_peak = {}
                logger.warning("Living World performance: the game's main thread stopped answering "
                               f"(LLM calls pending {row.get('llm_pending', 0)}, jobs running {row.get('jobs_running', 0)}, "
                               f"free memory {row.get('system_free_mb', '?')} MB)")
            for key in ("llm_pending", "jobs_running", "game_page_faults"):
                if isinstance(row.get(key), (int, float)):
                    self._stall_peak[key] = max(self._stall_peak.get(key, 0), row[key])
            row["stall"] = True
        elif self._stall_since is not None:
            seconds = now - self._stall_since
            logger.warning(f"Living World performance: the game answered again after about {seconds:.1f} s "
                           f"(during it: most LLM calls pending {self._stall_peak.get('llm_pending', 0)}, "
                           f"most jobs running {self._stall_peak.get('jobs_running', 0)}, "
                           f"page faults {self._stall_peak.get('game_page_faults', '?')})")
            row["stall_ended_s"] = round(seconds, 2)
            self._stall_since = None

    def _loop(self) -> None:
        with open(self.file_path, "a", encoding="utf-8") as out:
            while not self._stop.is_set():
                started = time.perf_counter()
                try:
                    row = self.sample_once()
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
                    out.flush()
                except Exception:
                    logger.exception("Living World performance monitor: sample failed")
                self._stop.wait(max(0.05, self.interval - (time.perf_counter() - started)))
