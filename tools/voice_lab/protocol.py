"""The guided Voice Lab protocol (about 5 minutes) and the utterance segmenter.

Steps: 10 s of room silence, one loud read (peak and clipping), the same neutral sentence 5 times (natural
spread), then the performed set (loud, quiet, fast, slow, hesitant, high, low), each twice.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from livingworld.voice.features import VocalFeatures

SENTENCE = "I need to speak with the Jarl about the dragon at the western watchtower."
LOUD_LINE = "Innkeeper! Two meads and a room for the night!"

PERFORMED_PROMPTS = {
    "loud": "Say it loudly and forcefully, as if across a noisy hall (no screaming).",
    "quiet": "Say it quietly, as if you don't want the guard next to you to hear.",
    "fast": "Say it fast, as if you're in a hurry.",
    "slow": "Say it slowly and deliberately, one word at a time.",
    "hesitant": "Say it hesitantly: stop and start, pause where you're unsure.",
    "high": "Say it in a noticeably higher voice than usual.",
    "low": "Say it in a noticeably lower, deeper voice than usual.",
}


@dataclass(frozen=True)
class Step:
    id: str
    kind: str            # "silence" or "take"
    label: str           # silence | loud_read | neutral | loud | quiet | ...
    prompt: str
    line: str = ""
    seconds: float = 0.0


def default_steps(neutral_repeats: int = 5, performed_repeats: int = 2) -> list[Step]:
    steps = [Step("silence", "silence", "silence", "Stay silent for 10 seconds (room noise).", seconds=10.0),
             Step("loud_read", "take", "loud_read", "Read this as loudly as you would call across a busy tavern "
                  "(no screaming). This checks the peak level and clipping.", LOUD_LINE)]
    for i in range(neutral_repeats):
        steps.append(Step(f"neutral_{i + 1}", "take", "neutral",
                          f"Say this in your normal voice ({i + 1} of {neutral_repeats}).", SENTENCE))
    for label, prompt in PERFORMED_PROMPTS.items():
        for i in range(performed_repeats):
            steps.append(Step(f"{label}_{i + 1}", "take", label, f"{prompt} ({i + 1} of {performed_repeats})",
                              SENTENCE))
    return steps


@dataclass
class Take:
    step_id: str
    label: str
    features: VocalFeatures
    wav: str | None = None
    transcript: str = ""
    at: float = field(default_factory=time.time)


class Protocol:
    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps = steps or default_steps()
        self.index = 0
        self.takes: list[Take] = []
        self.noise_floor_db: float | None = None

    @property
    def current(self) -> Step | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def done(self) -> bool:
        return self.index >= len(self.steps)

    def record_silence(self, noise_floor_db: float) -> None:
        if self.current is not None and self.current.kind == "silence":
            self.noise_floor_db = noise_floor_db
            self.index += 1

    def record(self, features: VocalFeatures, wav: str | None = None, transcript: str = "") -> Take | None:
        step = self.current
        if step is None or step.kind != "take":
            return None
        take = Take(step.id, step.label, features, wav, transcript)
        self.takes.append(take)
        self.index += 1
        return take

    def redo(self) -> Take | None:
        """Go back one step; the take recorded for it (if any) is dropped and returned."""
        if self.index == 0:
            return None
        self.index -= 1
        step = self.steps[self.index]
        if self.takes and self.takes[-1].step_id == step.id:
            return self.takes.pop()
        return None

    def skip(self) -> None:
        if not self.done:
            self.index += 1

    def features_for(self, label: str) -> list[VocalFeatures]:
        return [t.features for t in self.takes if t.label == label]

    def performed(self) -> dict[str, list[VocalFeatures]]:
        return {label: self.features_for(label) for label in PERFORMED_PROMPTS}


@dataclass
class Utterance:
    audio: np.ndarray
    sample_rate: int
    vad_probs: np.ndarray
    block_s: float
    started_at: float
    ended_at: float
    timed_out: bool = False


class Segmenter:
    """Cuts the block stream into utterances the way Mantella does: speech starts when the VAD probability
    passes the threshold (keeping 5 blocks of lookback), and ends after ``pause_s`` without speech or at
    ``max_s``. The trailing silence stays in the audio; the measurements trim it."""

    LOOKBACK_BLOCKS = 5

    def __init__(self, sample_rate: int, block_samples: int, threshold: float = 0.4, pause_s: float = 1.2,
                 max_s: float = 30.0) -> None:
        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self.block_s = block_samples / sample_rate
        self.threshold = threshold
        self.pause_s = pause_s
        self.max_s = max_s
        self.reset()

    def reset(self) -> None:
        self._blocks: list[np.ndarray] = []
        self._probs: list[float] = []
        self._speaking = False
        self._silent_blocks = 0
        self._started = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, block: np.ndarray, prob: float, now: float | None = None) -> Utterance | None:
        now = time.time() if now is None else now
        self._blocks.append(np.asarray(block, dtype=np.float32))
        self._probs.append(float(prob))
        if not self._speaking:
            if prob > self.threshold:
                self._speaking = True
                self._silent_blocks = 0
                self._started = now - (len(self._blocks) - 1) * self.block_s
            else:
                self._blocks = self._blocks[-self.LOOKBACK_BLOCKS:]
                self._probs = self._probs[-self.LOOKBACK_BLOCKS:]
            return None
        self._silent_blocks = 0 if prob > self.threshold else self._silent_blocks + 1
        timed_out = len(self._blocks) * self.block_s >= self.max_s
        if self._silent_blocks * self.block_s > self.pause_s or timed_out:
            utt = Utterance(np.concatenate(self._blocks), self.sample_rate, np.array(self._probs),
                            self.block_s, self._started, now, timed_out)
            self.reset()
            return utt
        return None
