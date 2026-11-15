# Voice Lab 0.1

A standalone tool that finds out which voice measurements are real on your mic: the mic you actually play with (the Quest 3 over Steam Link). It does not touch Mantella, Skyrim or the Living World database. Stage 1 of the vocal perception plan.

## Run it

From the MantellaExtension folder, in the same venv Mantella runs from:

```
pip install scipy praat-parselmouth      # optional, see below
python -m tools.voice_lab
```

Your browser opens `http://127.0.0.1:7861`. The page is served only to this PC and loads nothing from the internet.

| Option | What it does |
|---|---|
| `--device "Steam Streaming"` | pick the mic by (part of) its name; otherwise the last one you used |
| `--stt whisper:base.en` or `--stt moonshine:base` | also transcribe each take, for the transcript-based speaking rate. The model downloads once on first use; after that it runs locally. Off by default. |
| `--pause 2.0` | silence that ends an utterance (match your Mantella Pause Threshold) |
| `--no-praat` | use the built-in numpy pitch tracker |
| `--list-devices` | print input devices and exit |
| `--analyze a.wav b.wav` | measure 16-bit WAV files and print the numbers |
| `--data-dir <folder>` | store files somewhere else |

Optional packages: **SciPy** gives cleaner resampling when the mic runs at 44.1 or 48 kHz. **praat-parselmouth** (GPLv3+) gives Praat's pitch tracker. Without it, a numpy YIN tracker is used, which is slightly more prone to octave slips. Neither is needed to run.

## The protocol (about 5 minutes)

1. Pick the mic you play with and press **Use this mic**. Then **Start protocol**.
2. **Silence, 10 s.** Room noise floor.
3. **Loud read.** Check the clipped-samples counter stays at 0. If it doesn't, lower the mic level in Windows and **Redo last step**.
4. **The same neutral sentence 5 times.** This is your natural spread.
5. **Performed set**, each twice: loud, quiet, fast, slow, hesitant, high, low.

Each take ends after the pause, exactly as Mantella cuts your lines. Use **Redo last step** after a bad take, or **Skip step** to leave a step out. When the protocol finishes, the baseline is saved and the validation table appears. After that, anything you say is measured against your baseline and shown, but not saved ("free talk").

## The seven measurements

| # | Measurement | Notes |
|---|---|---|
| 1 | Duration and pauses | speech span with the trailing silence trimmed, voiced share, pauses of 250 ms or more |
| 2 | Loudness | median and 95th percentile in dBFS, relative to your baseline only |
| 3 | Clipping | clipped share and peak |
| 4 | SNR | speech level minus the noise floor |
| 5 | Pitch median and range | semitones re 100 Hz |
| 6 | Spectral tilt | energy above 1 kHz vs below: vocal effort, less affected by automatic gain |
| 7 | Speaking rate | acoustic syllable nuclei per second of speech; transcript-based too with `--stt` |

Every value carries a reliable / not reliable flag (too short, clipped, noisy, no transcript).

**Validation.** A measurement is **kept** only if a delivery that should move it (loud should raise loudness, slow should lower the rate...) moves it the right way by at least 3 times its natural spread. *Quality* rows (peak, clipping, SNR) describe the recording, not your voice. Pitch range is reported but not tested, because no protocol step targets it.

## Files (all on this PC)

`Documents\My Games\Mantella\data\audio\`

- `voice_lab_settings.json`: the mic you picked, by name
- `baseline\<mic>.json`: your baseline (numbers only); `<mic>.previous.json` is the one before it
- `voice_lab\<session>\measurements.csv`, `validation.csv`, `day_compare.csv`: numbers only, no transcripts
- `voice_lab\<session>\recordings\*.wav`: the protocol takes. **Delete recordings** removes every one of them from every session.

Free-talk lines are never written to disk. Only the CSV numbers are meant to be shared back.

## Acceptance tests

| ID | Test | Pass when |
|---|---|---|
| V1 | Pick the Steam Link mic by name, take the headset off and on | It keeps recording; the status shows "reconnecting" and then "recording" |
| V2 | Silence step + loud read | Noise floor and peak shown; 0 clipped samples after setting the Windows level |
| V3 | Neutral sentence 5 times | Validation shows a spread for each measurement |
| V4 | Performed set | The verdict column says which measurements are kept |
| V5 | Privacy | No WAV left after Delete recordings; the page loads nothing external; with `--stt` off the tool makes no network calls |
| V6 | Run the protocol again on another day | "Compared with the previous baseline": medians held within their spread |

Send back `validation.csv` (and `day_compare.csv` after the second day). That table decides which measurements Stage 2 may use in game.
