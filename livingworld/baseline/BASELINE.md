# Milestone 0 baseline checklist

The goal is to record how your PC and Mantella perform **before** any Living World change, so every
later milestone can be compared against real numbers. Plan for about 45 minutes of play.

## 1. Write down your setup

| Item | Value |
| --- | --- |
| CPU (Task Manager > Performance > CPU) | |
| RAM | 16 GB |
| GPU driver version | |
| Headset and render resolution (SteamVR > Video) | |
| Mantella version (window title or log) | |
| Mantella-Spell version (Mod Organizer) | |
| TTS service (Piper / xVASynth / XTTS / other) | |
| STT (Moonshine / Whisper, model size) | |
| LLM model in Mantella settings | |
| Mod count and load order tool | |

## 2. Run Mantella from your fork with recording on

In a terminal in the fork folder (with the Python 3.11 environment from the main README):

```
set MANTELLA_RECORD_TRAFFIC=1
python main.py
```

Recordings go to `Documents\My Games\Mantella\data\recordings\`. Nothing else changes in how Mantella behaves.

## 3. Start the load sampler

In a second terminal:

```
powershell -ExecutionPolicy Bypass -File .\livingworld\baseline\measure_baseline.ps1 -Label vanilla
```

The CSV goes to `Documents\My Games\Mantella\data\baseline\`. Stop it with Ctrl+C when you finish.

## 4. Play the baseline script

Turn on fpsVR (or SteamVR frame timing) and note frame rate and dropped frames in each scene.

| Scene | What to do | Frame rate (avg / min) | Notes (stutter, delays) |
| --- | --- | --- | --- |
| Riverwood, idle | Stand near the mill for 2 minutes, no conversation | | |
| Talk to Gerdur | 5 or more exchanges by voice; ask her age and favourite colour | | |
| Group talk | Add Hod to the conversation; 3 or more exchanges | | |
| End and leave | End the conversation, walk out of Riverwood | | |
| Wait 7 days | Use the wait menu or fast travel and come back | | |
| Talk to Gerdur again | Ask what she remembers about you | | |
| Radiant | Stand near two NPCs until they talk to each other | | |

## 5. Summarize response times

```
python -m livingworld.baseline.summarize_recording "%USERPROFILE%\Documents\My Games\Mantella\data\recordings\<file>.jsonl"
```

Note the median and slowest "player input -> first NPC line" times.

## 6. Send back

* the recording `.jsonl` file (it contains your conversation text, so only share it if you are comfortable)
* the baseline `.csv` file
* this checklist with the tables filled in
