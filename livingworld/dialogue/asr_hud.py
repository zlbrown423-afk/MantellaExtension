"""What the microphone recognition heard, shown in game (raw speech recognition HUD).

The text shown is exactly what Mantella's speech-to-text returned for the player's turn, before
anything the Living World does with it (misheard-speech notes, leads, guard words, ...). Those
only ever add notes for the NPCs; they never change the line, and neither does this module: it
only formats a copy for the game to show in the top-left notification corner.

Only lines that came from the microphone are shown. Typed lines (VR typing window, text input)
and the silence auto-response are not speech recognition, so they are never shown.

Switches (environment, read at server start):
  MANTELLA_LW_ASR_HUD=0            off (on by default with MANTELLA_LIVING_WORLD=1)
  MANTELLA_LW_ASR_HUD_SECONDS=8    how long the line stays up, 1-60 seconds
"""
from __future__ import annotations

import math
import os
from typing import Any

HUD_ENV = "MANTELLA_LW_ASR_HUD"
SECONDS_ENV = "MANTELLA_LW_ASR_HUD_SECONDS"
_OFF = {"0", "false", "no", "off"}

KEY_HEARD = "mantella_lw_asr_heard"   # string[]: the notification lines, in order
KEY_SHOWS = "mantella_lw_asr_shows"   # int: how many times the lines are shown in all
KEY_GAP = "mantella_lw_asr_gap"       # float: seconds between showings

DEFAULT_SECONDS = 8.0
MIN_SECONDS = 1.0
MAX_SECONDS = 60.0
# A Skyrim notification fades after a few seconds; to keep the line up longer it is shown again
# this many seconds later (as the guard duty reminders do).
GAP_SECONDS = 4.0
LINE_CHARS = 70     # a notification wider than this runs off the HUD
MAX_LINES = 3       # longer transcripts are cut, ending in "..."
PREFIX = "[Mic]"    # sets it apart from NPC subtitles, mood notes and overheard hints


def enabled() -> bool:
    return os.environ.get(HUD_ENV, "").strip().lower() not in _OFF


def display_seconds() -> float:
    raw = os.environ.get(SECONDS_ENV, "").strip()
    try:
        seconds = float(raw) if raw else DEFAULT_SECONDS
    except ValueError:
        seconds = DEFAULT_SECONDS
    if math.isnan(seconds):
        seconds = DEFAULT_SECONDS
    return min(MAX_SECONDS, max(MIN_SECONDS, seconds))


def shows_for(seconds: float) -> int:
    """How many showings, GAP_SECONDS apart, keep the line up for about `seconds`."""
    return max(1, math.ceil(seconds / GAP_SECONDS - 1e-9))


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > width:  # one very long "word": hard split
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def hud_lines(text: str) -> list[str]:
    """The notification lines for one recognised line. The words are the recogniser's own; only
    whitespace is collapsed (a notification shows one line) and very long lines are cut."""
    flat = " ".join((text or "").split())
    if not flat:
        return []
    parts = _wrap(flat, LINE_CHARS)
    if len(parts) > MAX_LINES:
        parts = parts[:MAX_LINES]
        parts[-1] = parts[-1] + " ..."
    if len(parts) == 1:
        return [f'{PREFIX} "{parts[0]}"']
    total = len(parts)
    out = []
    for i, part in enumerate(parts, 1):
        opening = '"' if i == 1 else ""
        closing = '"' if i == total else ""
        out.append(f"[Mic {i}/{total}] {opening}{part}{closing}")
    return out


def hud_keys(text: str | None, seconds: float | None = None) -> dict[str, Any]:
    """The reply keys that make the game show `text`, or {} when there is nothing to show."""
    lines = hud_lines(text or "")
    if not lines:
        return {}
    secs = display_seconds() if seconds is None else min(MAX_SECONDS, max(MIN_SECONDS, seconds))
    return {KEY_HEARD: lines, KEY_SHOWS: shows_for(secs), KEY_GAP: GAP_SECONDS}
