"""Summarizes response times in a traffic recording (see livingworld/harness/recorder.py).

Usage:
    python -m livingworld.baseline.summarize_recording <recording.jsonl> [more.jsonl ...]

Reports, per file:
* time from the player's input to the first NPC voiceline being ready (what you feel as lag)
* time per request type, as handled by Mantella
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from typing import Any

from livingworld.harness.recorder import load_recording

REQUEST_TYPE = "mantella_request_type"
REPLY_TYPE = "mantella_reply_type"
PLAYER_INPUT = "mantella_player_input"
NPC_TALK = "mantella_npc_talk"


def _stats(values: list[float]) -> str:
    if not values:
        return "no samples"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return f"n={len(values)}  median={statistics.median(values) / 1000:.2f}s  p90={p90 / 1000:.2f}s  max={values[-1] / 1000:.2f}s"


def first_line_latencies(exchanges: list[dict[str, Any]]) -> list[float]:
    """Milliseconds from the start of each player input request to the end of the first NPC line after it."""
    latencies: list[float] = []
    waiting_since: float | None = None
    for e in exchanges:
        request = e.get("request") or {}
        response = e.get("response") or {}
        if request.get(REQUEST_TYPE) == PLAYER_INPUT:
            waiting_since = e["t_ms"] - e["duration_ms"]
        elif waiting_since is not None and response.get(REPLY_TYPE) == NPC_TALK and response.get(NPC_TALK):
            latencies.append(e["t_ms"] - waiting_since)
            waiting_since = None
    return latencies


def summarize(path: str) -> str:
    exchanges = load_recording(path)
    by_type: dict[str, list[float]] = defaultdict(list)
    for e in exchanges:
        by_type[str((e.get("request") or {}).get(REQUEST_TYPE))].append(e["duration_ms"])
    lines = [path, f"  player input -> first NPC line: {_stats(first_line_latencies(exchanges))}"]
    for request_type, durations in sorted(by_type.items()):
        lines.append(f"  {request_type}: {_stats(durations)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    for path in argv:
        print(summarize(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
