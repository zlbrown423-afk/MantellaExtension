"""Voice Lab: python -m tools.voice_lab  (run from the MantellaExtension folder, in Mantella's venv).

    python -m tools.voice_lab                         open the page at http://127.0.0.1:7861
    python -m tools.voice_lab --stt whisper:base.en   also transcribe (text-based speaking rate)
    python -m tools.voice_lab --analyze a.wav b.wav   measure WAV files and print the numbers
    python -m tools.voice_lab --list-devices
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import wave
import webbrowser
from pathlib import Path

import numpy as np


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        sr, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return audio, sr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.voice_lab", description="Voice Lab (Living World vocal perception, Stage 1)")
    ap.add_argument("--port", type=int, default=7861)
    ap.add_argument("--data-dir", help="where numbers and recordings go (default Documents\\My Games\\Mantella\\data\\audio)")
    ap.add_argument("--device", help="input device name (or part of it); default: the last one used")
    ap.add_argument("--stt", default="none", help="none | whisper:<model> | moonshine:<tiny|base>")
    ap.add_argument("--no-praat", action="store_true", help="use the numpy pitch tracker even if parselmouth is installed")
    ap.add_argument("--pause", type=float, default=1.2, help="seconds of silence that end an utterance (Mantella's Pause Threshold)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--analyze", nargs="+", metavar="WAV")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.analyze:
        from livingworld.voice.features import analyze
        for path in args.analyze:
            audio, sr = read_wav(path)
            feats = analyze(audio, sr, prefer_praat=not args.no_praat)
            print(json.dumps({"file": path, **feats.to_row()}))
        return 0

    from tools.voice_lab.capture import list_devices
    if args.list_devices:
        for d in list_devices():
            print(f"{d.index:3d}  {d.name}  [{d.hostapi}, {d.sample_rate} Hz]{'  (Windows default)' if d.is_default else ''}")
        return 0

    from tools.voice_lab.lab import VoiceLab
    from tools.voice_lab.server import HOST, serve
    from tools.voice_lab.storage import Storage

    stt = None
    if args.stt and args.stt.lower() != "none":
        from tools.voice_lab.stt import Transcriber
        print(f"Loading speech-to-text {args.stt} ...")
        stt = Transcriber(args.stt)

    storage = Storage(Path(args.data_dir) if args.data_dir else None)
    lab = VoiceLab(storage, stt=stt, prefer_praat=not args.no_praat, pause_s=args.pause)
    lab.refresh_devices()
    wanted = args.device or storage.load_settings().get("device")
    if wanted:
        lab.select_device(wanted, None if args.device else storage.load_settings().get("hostapi"))

    server = serve(lab, args.port)
    url = f"http://{HOST}:{args.port}/"
    print(f"Voice Lab is running at {url}  (Ctrl+C to quit). Files: {storage.root}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if lab.capture is not None:
            lab.capture.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
