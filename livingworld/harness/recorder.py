"""Records the game's /mantella traffic to a JSON Lines file.

Each line is one request/response pair, so a real play session can be replayed
later without the game, and the timings give a latency baseline.

Recording is off unless the environment variable ``MANTELLA_RECORD_TRAFFIC`` is
set before Mantella starts:

* ``MANTELLA_RECORD_TRAFFIC=1`` writes to ``<Mantella save folder>/data/recordings``
* ``MANTELLA_RECORD_TRAFFIC=<folder path>`` writes to that folder instead

A new file is started each time Mantella starts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

ENV_VAR = "MANTELLA_RECORD_TRAFFIC"
RECORDING_FORMAT_VERSION = 1


class TrafficRecorder:
    """Appends request/response pairs to a JSONL file. Safe to call from several threads."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self._lock = threading.Lock()
        self._seq = 0
        self._start = time.perf_counter()
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        self._write({
            "kind": "header",
            "format_version": RECORDING_FORMAT_VERSION,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })

    @staticmethod
    def from_environment(save_folder: str) -> "TrafficRecorder | None":
        """Returns a recorder if recording is switched on via the environment, else None."""
        value = os.environ.get(ENV_VAR, "").strip()
        if not value or value.lower() in ("0", "false", "no", "off"):
            return None
        if value.lower() in ("1", "true", "yes", "on"):
            folder = os.path.join(save_folder, "data", "recordings")
        else:
            folder = value
        file_name = datetime.now().strftime("session_%Y-%m-%d_%H-%M-%S.jsonl")
        return TrafficRecorder(os.path.join(folder, file_name))

    def record(self, request: dict[str, Any] | None, response: dict[str, Any], duration_ms: float) -> None:
        with self._lock:
            self._seq += 1
            self._write({
                "kind": "exchange",
                "seq": self._seq,
                "t_ms": round((time.perf_counter() - self._start) * 1000, 1),
                "duration_ms": round(duration_ms, 1),
                "request": request,
                "response": response,
            })

    def _write(self, entry: dict[str, Any]) -> None:
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_recording(file_path: str) -> list[dict[str, Any]]:
    """Returns the exchanges in a recording, in order (the header line is skipped)."""
    exchanges: list[dict[str, Any]] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("kind") == "exchange":
                exchanges.append(entry)
    return exchanges
