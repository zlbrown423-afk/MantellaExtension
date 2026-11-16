"""Where one conversation turn's wait goes, from the mic opening to the NPC's first line reaching the game.

The player's 5 to 8 second wait between exchanges is spread over several places that each log their own
timing (or none): the silence the mic waits for, the transcription, the AI's first token and first full
sentence, the voice synthesis of that sentence and the hand-over to the game. This module ties those
moments of ONE turn together and logs a single line when the first line of the reply is handed to the
game, e.g.::

    Living World turn timing (Fralia Gray-Mane, mic): mic open to speech 2.31 s | speech to pause 3.10 s |
    pause to transcript 0.62 s | transcript to AI request 0.004 s | AI request to first token 0.81 s |
    first token to first sentence 0.40 s | first sentence to voiced 0.55 s | voiced to game 0.02 s |
    wait after speaking 2.41 s

Each hook is one ``TURN.mark(...)`` call: a ``perf_counter`` read and a dict write under a lock, kept
only the first time a phase is seen in a turn. Nothing here changes what the server does.

Off: ``MANTELLA_LW_TURN_TIMING=0``.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from src import utils

logger = utils.get_logger()

ENV_VAR = "MANTELLA_LW_TURN_TIMING"

# The phases of one turn, in the order they happen. A turn may miss some (typed input has no mic phases,
# an interruption hears speech before the mic-open mark).
MIC_OPEN = "mic_open"            # the server starts waiting for the player's speech
SPEECH = "speech"                # voice activity detected
PAUSE = "pause"                  # the silence threshold passed (or push-to-talk released): transcription starts
TRANSCRIPT = "transcript"        # the player's words are known (spoken or typed)
AI_REQUEST = "ai_request"        # the first dialogue request of the reply is sent
FIRST_TOKEN = "first_token"      # its first streamed token arrived
FIRST_SENTENCE = "first_sentence"  # the first whole sentence is parsed out of the stream
VOICED = "voiced"                # that sentence's voice line is synthesised and queued
LINE_SENT = "line_sent"          # the first NPC line is handed to the game

PHASES = (MIC_OPEN, SPEECH, PAUSE, TRANSCRIPT, AI_REQUEST, FIRST_TOKEN, FIRST_SENTENCE, VOICED, LINE_SENT)
LABELS = {MIC_OPEN: "mic open", SPEECH: "speech", PAUSE: "pause", TRANSCRIPT: "transcript",
          AI_REQUEST: "AI request", FIRST_TOKEN: "first token", FIRST_SENTENCE: "first sentence",
          VOICED: "voiced", LINE_SENT: "game"}

# A reply request that is re-sent with fresh game events (the player took long to speak) continues the
# same turn when it comes back within this many seconds.
CONTINUE_WITHIN_S = 30.0


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() not in ("0", "false", "no", "off")


class TurnTimeline:
    """The marks of the current turn and the last few finished ones (for tests and the perf monitor)."""

    def __init__(self, clock: Any = time.perf_counter, keep: int = 50) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._marks: dict[str, float] | None = None
        self._input = ""
        self.finished: deque[dict[str, Any]] = deque(maxlen=keep)

    def start(self, mic: bool) -> None:
        """The game asked for the player's line. ``mic``: the server will listen for it."""
        if not enabled():
            return
        now = self._clock()
        with self._lock:
            m = self._marks
            if m and TRANSCRIPT in m and LINE_SENT not in m and now - m[TRANSCRIPT] < CONTINUE_WITHIN_S:
                return   # the same line, re-sent with fresh game events
            if (m and TRANSCRIPT not in m and SPEECH in m and mic
                    and now - m[SPEECH] < CONTINUE_WITHIN_S):
                return   # the player is already talking over the NPC (an interruption): keep what was heard
            self._marks = {MIC_OPEN: now} if mic else {}
            self._input = "mic" if mic else "typed"

    def mark(self, phase: str) -> None:
        """Note that ``phase`` happened now; only its first time in a turn counts."""
        if not enabled():
            return
        now = self._clock()
        with self._lock:
            if self._marks is None:
                if phase not in (SPEECH, PAUSE):
                    return
                # an interruption: the player spoke over the NPC before asking to be heard
                self._marks, self._input = {}, "mic"
            if phase in self._marks or LINE_SENT in self._marks:
                return
            if phase == TRANSCRIPT and not self._marks:
                self._input = "typed"
            self._marks[phase] = now

    def cancel(self) -> None:
        """The conversation ended or the turn was dropped: nothing to report."""
        with self._lock:
            self._marks = None

    def line_sent(self, speaker: str | None) -> dict[str, Any] | None:
        """The first NPC line of the reply reached the game: log the turn and start fresh.
        Returns the finished record (None when there was no turn in progress)."""
        if not enabled():
            return None
        now = self._clock()
        with self._lock:
            m = self._marks
            if not m or TRANSCRIPT not in m:
                return None   # a greeting, or a line that does not answer the player
            m[LINE_SENT] = now
            record = {"speaker": speaker or "?", "input": self._input, "marks": dict(m),
                      "gaps": gaps(m)}
            self._marks = None
            self.finished.append(record)
        logger.log(23, describe(record))
        return record


def gaps(marks: dict[str, float]) -> list[tuple[str, str, float]]:
    """(from, to, seconds) between consecutive marks that were seen, in phase order. A mark earlier than the
    one before it (an interruption heard speech before the mic-open mark) is skipped."""
    out: list[tuple[str, str, float]] = []
    last: tuple[str, float] | None = None
    for phase in PHASES:
        at = marks.get(phase)
        if at is None:
            continue
        if last is not None:
            if at < last[1]:
                continue
            out.append((last[0], phase, at - last[1]))
        last = (phase, at)
    return out


def _secs(s: float) -> str:
    return f"{s:.3f} s" if s < 0.1 else f"{s:.2f} s"


def describe(record: dict[str, Any]) -> str:
    parts = [f"{LABELS[a]} to {LABELS[b]} {_secs(s)}" for a, b, s in record["gaps"]]
    marks = record["marks"]
    start = marks.get(PAUSE, marks.get(TRANSCRIPT))
    if start is not None and LINE_SENT in marks:
        parts.append(f"wait after speaking {_secs(marks[LINE_SENT] - start)}")
    return f"Living World turn timing ({record['speaker']}, {record['input']}): " + " | ".join(parts)


TURN = TurnTimeline()
