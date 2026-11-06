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

## Milestone 1: persistent NPC memory (in progress)

### PR A: SQLite store, stable identity, raw conversation log

| Piece | File | What it does |
| --- | --- | --- |
| World store | `store/world_store.py` | One SQLite file per Mantella world (`data/livingworld/<world>.sqlite`), WAL mode, schema migrations from `store/migrations/` |
| Schema | `store/migrations/001_initial.sql` | The full Living World schema (identity, conversations, knowledge, memories, timelines, jobs) with its append-only and voice-isolation triggers |
| Identity | `store/identity.py` | One `npc` row per actor, keyed by plugin and full form IDs (never by name); locks each NPC's voice the first time it speaks |
| Conversation log | `store/conversation_log.py` | Stores every spoken line as it is played, with speaker, voice and game time. Rows can't be edited or deleted |
| Mantella hooks | `integration.py` | Called from `GameStateManager`; only records, and any error is logged and ignored so the game never waits on it |

Turn it on with `set MANTELLA_LIVING_WORLD=1` before `python main.py`. It is off by default until Milestone 1 is finished.
Mantella's own summaries keep working as before.

Until the game sends plugin file names (PR D), actors outside Skyrim.esm get a placeholder plugin such as
`load_index:05`, which stays stable as long as your load order does.

### Upstream patches

| File | Change |
| --- | --- |
| `src/http/routes/mantella_route.py` | Optional traffic recording (does nothing unless `MANTELLA_RECORD_TRAFFIC` is set) |
| `tests/conftest.py` | Registers the `baseline_behaviour` test marker |
| `src/game_manager.py` | Calls the Living World hooks at conversation start, each spoken line, player input, actor changes and conversation end (does nothing unless `MANTELLA_LIVING_WORLD` is set) |
