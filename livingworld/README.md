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

### PR B: per-NPC memory extraction and recall

| Piece | File | What it does |
| --- | --- | --- |
| Job queue and worker | `memory/jobs.py` | Ending a conversation only queues a job; a background thread runs it. Failed jobs retry after 5 s, 30 s, 2 min, 10 min, 30 min, then stop as `dead`. Jobs left running by a crash restart on the next launch |
| Extraction | `memory/extraction.py` | One AI call per NPC who took part, from that NPC's point of view, using only the lines they heard. Each fact must name the line it came from and who said it, or it is dropped. Facts become `knowledge` rows (`self`, or `told` with the teller and the teller's own copy linked); impressions become `memory` rows |
| Recall | `memory/recall.py`, `memory/remembering.py` | Adds each NPC's own facts and memories (with "about 7 days ago" timing) to Mantella's prompt, reading only that NPC's rows through the timeline views |

Extraction uses Mantella's summary model if one is set, otherwise the main model. One conversation costs one
extraction call per NPC in it.

### PR C: one NPC per AI call in group talk, voice check

| Piece | File | What it does |
| --- | --- | --- |
| Group turns | `dialogue/group.py` | Each NPC gets their own AI call with their own one-on-one prompt (their bio and their recall only) plus a note naming who else is present. Others' lines are shown as "Name: text". A reply that tries to speak as someone else is cut off there |
| Who speaks | `dialogue/group.py` | The NPC the player names first, otherwise the one who did not speak last. If the speaker names another NPC, that NPC answers next, up to 2 speakers per player turn |
| Voice check | `integration.py` (`voice_allowed`) | Before synthesis, the voice Mantella picked must match the NPC's bound voice, or no audio is made and the error is logged |
| Call log | `integration.py` (`record_dialogue_call`) | One `llm_request` row per dialogue call with the speaker, and a `decision_trace` row listing the knowledge and memory ids that call's prompt held |

Radiant conversations (two NPCs, no player) still use Mantella's own single call for now.

### PR D: save reloads and plugin names (server side, with the Mantella-Spell `living-world-m1` branch)

| Piece | File | What it does |
| --- | --- | --- |
| Timelines | `store/timeline.py`, `store/migrations/002_game_load.sql` | The game sends, with each conversation start, the timeline its save was on, a random number picked at load, and the save's game time. If that timeline has rows from after the save, a new timeline branches at the save's game time, so NPCs forget what came after it. Loading the later save makes its timeline active again. Nothing is deleted |
| Plugin names | `store/identity.py` | Uses `mantella_actor_base_plugin` / `mantella_actor_ref_plugin` from the game when present, instead of the load-order placeholder |

The reply to a conversation start carries `mantella_lw_timeline_id`, which the game keeps in its save.
Without the game-side change nothing new is sent, and everything stays on one timeline.

## Perception prototype: grabs, eating and drinking, witnesses

NPCs remember being grabbed by the player and seeing it happen, and seeing someone eat or drink,
with no LLM call. Game side: the Living World build of Mantella-Spell (`MantellaRepository.psc`).

| Piece | File | What it does |
| --- | --- | --- |
| Records | `perception/records.py` | Parses the text records the game queues under the context key `mantella_lw_events` (format in the module docstring); maps skeleton nodes to body parts and items to food, drink, alcohol or potion |
| Who perceived it | `perception/formation.py` | A nearby NPC "saw" the event only with line of sight and within 20 m; confidence falls from 0.95 at 8 m to 0.5 at 20 m |
| Memories | `perception/formation.py` | One first-person memory per NPC, by role: experienced (the person grabbed), witnessed (a bystander), or did it. Each has an interpretation from local rules (relationship, duration, body part); a witness never learns why |
| Storage | `perception/pipeline.py`, `store/migrations/003_perception.sql` | One `world_event` (objective record), `event_witness` rows, `memory` rows with `source_kind`, `interpretation`, `confidence`, `event_id`; duplicates dropped by the game's event key |
| Reactions | `perception/pipeline.py` | Small trust changes as `relationship_change` rows; `v_relationship` sums them on the active timeline, so a reload undoes them |
| Recall | `memory/recall.py` | Marks each memory "it happened to me" or "I saw it myself", adds what the NPC made of it, and lets the NPC raise a fresh one unprompted |
| Simulation | `harness/perception.py` | Builds records exactly as the game does, for tests without Skyrim |

Not detected yet: touching without grabbing, pushing and bumps (HIGGS and PLANCK do not expose them),
NPCs eating, and environmental events. Records reach the server with the next Mantella request, so a
memory forms when a conversation next starts or continues.

## Actions: sit, stand up, go there, drink

Four extra actions the LLM can use, offered only with `MANTELLA_LIVING_WORLD=1`. They are ordinary
action files (`actions/*.json`, same format as `data/actions/`), loaded by `FunctionManager.load_all_actions`.
They work in Mantella's basic mode ("Sit: ..." at the start of a reply) and with advanced actions on.
Refusing is not calling the action, so each description tells the NPC it may refuse in character.

| Action | Game side (Mantella-Spell `MantellaRepository.psc`) |
| --- | --- |
| `Sit` (furniture: pointed, seat, chair, bench, stool, bed) | The furniture the player last pointed at in VR (within 20 s), else the nearest free one of that kind in the cell, found by name. The NPC walks there, sits or lies down, and sits back down if their AI makes them get up, for up to 3 minutes |
| `StandUp` | Ends a sit or stay order and gets the NPC out of the furniture |
| `GoThere` | Walks to the last non-person thing the player pointed at and stays near it for up to 3 minutes (a pointed seat means Sit). People: use upstream `MoveTo` |
| `Drink` (optional drink name) | Drinks from the NPC's own inventory, so the player hands drinks over with `Inventory` first. Queues a `consume` perception record (`via=npc_action`), so witnesses remember it and it counts towards being tipsy |

When there is nothing to sit on, nowhere pointed at, or nothing to drink, the game adds an in-game event
line so the NPC can say so.

### Upstream patches

| File | Change |
| --- | --- |
| `src/http/routes/mantella_route.py` | Optional traffic recording (does nothing unless `MANTELLA_RECORD_TRAFFIC` is set) |
| `src/actions/function_manager.py` | Also loads `livingworld/actions/*.json` (does nothing unless `MANTELLA_LIVING_WORLD` is set) |
| `tests/conftest.py` | Registers the `baseline_behaviour` test marker |
| `src/output_manager.py` | Hands each turn to the Living World when it is on (group turns), refuses synthesis in the wrong voice, and keeps the "generating" flag up across a multi-speaker turn |
| `src/conversation/context.py` | `focused_on(character)`: a copy of the context holding only the player and one NPC, for building that NPC's own prompt |
| `src/game_manager.py` | Calls the Living World hooks at conversation start, on each request's context (perception records), each spoken line, player input, actor changes and conversation end, and wraps Mantella's memory with the recall (does nothing unless `MANTELLA_LIVING_WORLD` is set) |
