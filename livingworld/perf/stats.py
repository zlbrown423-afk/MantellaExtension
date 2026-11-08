"""Counters the server keeps about its own work, for the performance monitor (see ``monitor.py``).

Every counter is a few integer additions under one lock, so they are always on: the monitor only
reads them. Nothing here changes what the server does.

* LLM calls: how many are waiting for the client lock or in flight right now, by kind
  (``dialogue`` streaming replies, ``request`` one-shot calls such as memory extraction).
* Game requests: how many ``/mantella`` requests finished and the slowest one, by request type.
* Perception records: how many arrived from the game, by type, and how long storing them took.
* Background jobs: how many are running and how many finished.
* Game markers: the timing summaries the game sends (``type=perf`` records; see ``game_records.py``).
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


class ServerStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_pending: dict[str, int] = {}
        self._reset_window()
        self.markers: list[dict[str, Any]] = []   # game markers not yet written by the monitor

    def _reset_window(self) -> None:
        self.llm_started = 0
        self.llm_finished = 0
        self.llm_max_ms = 0.0
        self.llm_peak_pending = 0
        self.requests: dict[str, int] = {}
        self.request_max_ms = 0.0
        self.request_max_type = ""
        self.records: dict[str, int] = {}
        self.ingest_ms = 0.0
        self.jobs_running = getattr(self, "jobs_running", 0)
        self.jobs_done = 0
        self.jobs_peak_running = self.jobs_running

    # ---- LLM ----
    @contextmanager
    def llm_call(self, kind: str) -> Iterator[None]:
        """Wrap one LLM call, from the moment it waits for the client lock until it returns."""
        start = time.perf_counter()
        with self._lock:
            self.llm_pending[kind] = self.llm_pending.get(kind, 0) + 1
            self.llm_started += 1
            self.llm_peak_pending = max(self.llm_peak_pending, sum(self.llm_pending.values()))
        try:
            yield
        finally:
            ms = (time.perf_counter() - start) * 1000
            with self._lock:
                self.llm_pending[kind] = max(0, self.llm_pending.get(kind, 0) - 1)
                self.llm_finished += 1
                self.llm_max_ms = max(self.llm_max_ms, ms)

    # ---- game requests ----
    def request_done(self, request_type: str, ms: float) -> None:
        with self._lock:
            self.requests[request_type] = self.requests.get(request_type, 0) + 1
            if ms > self.request_max_ms:
                self.request_max_ms = ms
                self.request_max_type = request_type

    # ---- perception ----
    def records_ingested(self, kinds: dict[str, int], ms: float) -> None:
        with self._lock:
            for kind, n in kinds.items():
                self.records[kind] = self.records.get(kind, 0) + n
            self.ingest_ms += ms

    # ---- background jobs ----
    def job_started(self) -> None:
        with self._lock:
            self.jobs_running += 1
            self.jobs_peak_running = max(self.jobs_peak_running, self.jobs_running)

    def job_finished(self) -> None:
        with self._lock:
            self.jobs_running = max(0, self.jobs_running - 1)
            self.jobs_done += 1

    # ---- game markers ----
    def add_marker(self, marker: dict[str, Any]) -> None:
        with self._lock:
            self.markers.append(marker)
            del self.markers[:-200]

    def take_markers(self) -> list[dict[str, Any]]:
        with self._lock:
            taken, self.markers = self.markers, []
            return taken

    # ---- reading ----
    def pending_llm(self) -> int:
        with self._lock:
            return sum(self.llm_pending.values())

    def take_window(self) -> dict[str, Any]:
        """Everything counted since the last call, then start a new window."""
        with self._lock:
            window = {
                "llm_pending": sum(self.llm_pending.values()),
                "llm_pending_by_kind": {k: v for k, v in self.llm_pending.items() if v},
                "llm_peak_pending": self.llm_peak_pending,
                "llm_started": self.llm_started,
                "llm_finished": self.llm_finished,
                "llm_max_ms": round(self.llm_max_ms, 1),
                "requests": dict(self.requests),
                "request_max_ms": round(self.request_max_ms, 1),
                "request_max_type": self.request_max_type,
                "records": dict(self.records),
                "ingest_ms": round(self.ingest_ms, 1),
                "jobs_running": self.jobs_running,
                "jobs_peak_running": self.jobs_peak_running,
                "jobs_done": self.jobs_done,
            }
            self._reset_window()
            self.llm_peak_pending = sum(self.llm_pending.values())
            return window


STATS = ServerStats()
