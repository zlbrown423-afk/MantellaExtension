"""Timing summaries the game sends, and how they line up with the monitor's own clock.

The Living World build of Mantella-Spell watches its own script timing (see ``MantellaRepository.psc``,
"LIVINGWORLD PATCH: performance probe") and queues ``type=perf`` records with the perception records::

    v=1|key=812345-9|type=perf|gt=3.51|loc=Riverwood|what=grab|hand=L|target=Gerdur|rt0=812.4|rt1=871.9
      |grabs=1|drops=1|dups=0|short=0|ticks=118|lag_avg=0.012|lag_max=19.84|stalls=1|grab_ms=41

    v=1|key=812345-10|type=perf|gt=3.52|loc=Riverwood|what=stall|rt0=902.2|lag=3.1|held=0

``what=grab``: one hold of an NPC from grab to release. ``rt0``/``rt1`` are the game's real-time clock
(seconds since the game started) at grab and release. ``grabs``/``drops`` count every HIGGS grab and drop
event for that hand during the hold (a healthy hold is 1 and 1), ``dups`` the repeated grab events that were
ignored, ``short`` holds too short to count. ``ticks`` is how many times the probe ran (every half second
while something is held); ``lag_avg``/``lag_max`` are how late, in seconds, each tick ran against when it
was asked for, and ``stalls`` how many ticks ran more than a second late. ``grab_ms`` is how long working out
the grabbed body area took, game real time, and ``noarea`` how many grabs skipped it because another was
still being worked out (each bone position read waits for the game's main thread).

``what=stall``: a regular script tick outside the probe ran ``lag`` seconds late; ``held`` = 1 if an NPC
was being held at the time.

A late tick cannot tell on its own whether the whole game stopped (main thread) or only scripts were
slow; the monitor's window probe and frame timings can. The game also sends its current real-time
clock with each request under ``mantella_lw_rt``, so each marker can be placed on the wall clock.
"""
from __future__ import annotations

import time
from typing import Any

KEY_REALTIME = "mantella_lw_rt"
_NUMBERS = ("rt0", "rt1", "grabs", "drops", "dups", "short", "ticks", "lag_avg", "lag_max", "stalls", "grab_ms",
            "lag", "held", "gt", "noarea")


def is_perf_record(raw: str) -> bool:
    return "|type=perf|" in raw.lower().replace(" ", "") + "|"


def parse_perf_record(raw: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for part in raw.split("|"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip().lower()
        if name in _NUMBERS:
            try:
                fields[name] = float(value)
            except ValueError:
                continue
        else:
            fields[name] = value.strip()
    if "what" in fields:
        fields["what"] = str(fields["what"]).lower()   # Papyrus may recase string literals
    return fields


def place_on_wall_clock(marker: dict[str, Any], game_now: float | None, wall_now: float | None = None) -> dict[str, Any]:
    """Add ``t0``/``t1`` (Unix seconds) from ``rt0``/``rt1`` when the game sent its clock with the request."""
    wall_now = time.time() if wall_now is None else wall_now
    if game_now is None:
        return marker
    for rt_key, wall_key in (("rt0", "t0"), ("rt1", "t1")):
        if isinstance(marker.get(rt_key), float):
            marker[wall_key] = round(wall_now - (game_now - marker[rt_key]), 3)
    return marker


def split_perf_records(raw_records: list[str], context: dict[str, Any] | None,
                       wall_now: float | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Separate the game's timing records from the perception records; returns (perception, markers)."""
    game_now = None
    if context and context.get(KEY_REALTIME) is not None:
        try:
            game_now = float(context[KEY_REALTIME])
        except (TypeError, ValueError):
            game_now = None
    keep: list[str] = []
    markers: list[dict[str, Any]] = []
    for raw in raw_records:
        if is_perf_record(raw):
            markers.append(place_on_wall_clock(parse_perf_record(raw), game_now, wall_now))
        else:
            keep.append(raw)
    return keep, markers
