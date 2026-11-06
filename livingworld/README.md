# Living World

New code for the persistent NPC simulation. It lives outside `src/` so the upstream Mantella code
only needs small patches, each marked with a `LIVINGWORLD PATCH` comment.

## Milestone 0: baseline and test harness

| Piece | File | What it does |
| --- | --- | --- |
| Traffic recorder | `harness/recorder.py` | Saves every `/mantella` request and reply, with timings, to a JSONL file when `MANTELLA_RECORD_TRAFFIC` is set. Off by default. |
| Scripted LLM | `harness/scripted_llm.py` | Replaces the LLM with scripted replies and keeps every prompt it receives. No API calls. |
| Stub TTS | `harness/stub_tts.py` | Writes silent WAV files and logs which voice spoke each line. |
| Headless session | `harness/session.py` | Runs real Mantella conversations the way the game does (start, continue, player input, end), offline. Can also replay a recording. |
| Test actors | `harness/actors.py` | Player, Gerdur and Hod payloads shaped like the game's. |
| Load sampler | `baseline/measure_baseline.ps1` | Windows script that logs RAM, CPU, GPU and VRAM use while you play. |
| Latency summary | `baseline/summarize_recording.py` | Player-input-to-first-line times from a recording. |
| Baseline checklist | `baseline/BASELINE.md` | The in-game steps for recording the baseline. |

Run the tests:

```
pytest tests/livingworld -q
```

Tests marked `baseline_behaviour` describe known problems in upstream Mantella (for example, one
prompt holding every NPC's memories in a group conversation). Milestone 1 is expected to change them.

### Upstream patches

| File | Change |
| --- | --- |
| `src/http/routes/mantella_route.py` | Optional traffic recording (does nothing unless `MANTELLA_RECORD_TRAFFIC` is set) |
| `tests/conftest.py` | Registers the `baseline_behaviour` test marker |
