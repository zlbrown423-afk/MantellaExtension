"""Where Voice Lab keeps things, all on this PC: Documents\\My Games\\Mantella\\data\\audio\\

    audio/
      voice_lab_settings.json         chosen mic (by name)
      baseline/<mic>.json             current baseline (numbers only)
      baseline/<mic>.previous.json    the one before it (for the day-to-day check)
      voice_lab/<session>/
        measurements.csv              one row per take, numbers only
        validation.csv                which measurements are kept
        day_compare.csv               second-day check, when there was an earlier baseline
        recordings/*.wav              protocol takes; removed by Delete recordings
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import time
import wave
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def documents_folder() -> Path:
    if sys.platform == "win32":
        try:  # the real Documents folder, which may live in OneDrive
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def default_root() -> Path:
    return documents_folder() / "My Games" / "Mantella" / "data" / "audio"


def mic_slug(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "mic")[:60]


class Storage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_root()
        self.session_dir = self.root / "voice_lab" / time.strftime("%Y%m%d-%H%M%S")
        self.recordings = self.session_dir / "recordings"

    # settings -------------------------------------------------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return self.root / "voice_lab_settings.json"

    def load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # baselines ------------------------------------------------------------------------------------------
    def baseline_path(self, mic: str) -> Path:
        return self.root / "baseline" / f"{mic_slug(mic)}.json"

    def previous_baseline_path(self, mic: str) -> Path:
        return self.root / "baseline" / f"{mic_slug(mic)}.previous.json"

    def archive_baseline(self, mic: str) -> bool:
        """Keep the current baseline as the previous one before a new one replaces it."""
        cur = self.baseline_path(mic)
        if not cur.exists():
            return False
        shutil.copyfile(cur, self.previous_baseline_path(mic))
        return True

    # recordings -----------------------------------------------------------------------------------------
    def save_wav(self, name: str, audio: np.ndarray, sample_rate: int) -> Path:
        self.recordings.mkdir(parents=True, exist_ok=True)
        path = self.recordings / f"{name}.wav"
        pcm = (np.clip(np.asarray(audio, dtype=np.float64), -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm.tobytes())
        return path

    def recordings_on_disk(self) -> list[Path]:
        base = self.root / "voice_lab"
        return sorted(base.glob("*/recordings/*.wav")) if base.exists() else []

    def delete_recordings(self) -> int:
        """Remove every Voice Lab recording from every session. Numbers (CSV, baselines) stay."""
        files = self.recordings_on_disk()
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        for d in {f.parent for f in files}:
            try:
                d.rmdir()
            except OSError:
                pass
        return len(files) - len(self.recordings_on_disk())

    # numbers --------------------------------------------------------------------------------------------
    def write_csv(self, name: str, rows: Iterable[dict[str, Any]], append: bool = False) -> Path:
        rows = list(rows)
        path = self.session_dir / name
        if not rows:
            return path
        self.session_dir.mkdir(parents=True, exist_ok=True)
        new = not (append and path.exists())
        with open(path, "a" if append else "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if new:
                writer.writeheader()
            writer.writerows(rows)
        return path


def open_folder(path: Path) -> None:  # pragma: no cover - Windows convenience
    if sys.platform == "win32" and path.exists():
        os.startfile(str(path))  # type: ignore[attr-defined]
