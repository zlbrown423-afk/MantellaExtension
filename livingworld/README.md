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

## Clothing awareness and drinking

Both are perception changes. Neither changes a relationship, and drink never changes what an NPC is
willing to do: every prompt that mentions drink says that anything the NPC would refuse sober, such as
undressing or intimacy, they still refuse.

| Piece | File | What it does |
| --- | --- | --- |
| Attire records | `perception/records.py` | Type `attire`: what one person wears in the body slot (`cover` = clothing, armor or none). The game sends one per nearby person with every request (`mantella_lw_attire`) and queues one when the player takes off or puts on body clothing |
| Attire memory | `perception/attire.py` | Stores only changes, as `attire` world events on the active timeline. Someone seen with nothing in the body slot gives each NPC who could see them (line of sight, within 20 m) a memory, and the NPC themself one too. NPCs who arrive later while it lasts get the same event once. Dressing again closes it |
| Drink action | `data/actions/livingworld_drink.json` | "Drink": the NPC drinks ale, mead, wine and the like from their own inventory. Only loaded with `MANTELLA_LIVING_WORLD=1` (actions marked `living_world`) |
| Intoxication | `perception/intoxication.py` | Counts the NPC's own drinks (skooma does not count); about 0.35 drinks wear off per game hour. Stages merry, tipsy, drunk, very drunk change speech, mood and social behaviour: louder, looser lips with gossip and secrets, singing and dancing, affection or quarrels by temperament. Memories formed while drunk are marked hazy and less certain |
| What an NPC notices | `perception/awareness.py` | Adds to each NPC's own prompt: who present has no clothes or armor on (including themself), their own drink stage, how many drinks they saw others present have, and which other adults are nearby but not in the conversation (so they can judge privacy) |
| Chosen undress | `perception/attire.py` | An attire record with `via=action` (the Undress action) is remembered as the NPC's own choice: "I chose to take off my clothes" for them, "I saw Gerdur take off their clothes" for witnesses within sight |

## Actions: sit, stand up, go there, order drinks, undress, dress

Extra actions the LLM can use, offered only with `MANTELLA_LIVING_WORLD=1`. They are ordinary
action files (`actions/*.json`, same format as `data/actions/`), loaded by `FunctionManager.load_all_actions`.
They work in Mantella's basic mode ("Sit: ..." at the start of a reply) and with advanced actions on.
Refusing is not calling the action, so each description tells the NPC it may refuse in character.
Drinking is the `Drink` action above.

| Action | Game side (Mantella-Spell `MantellaRepository.psc`) |
| --- | --- |
| `Sit` (furniture: pointed, seat, chair, bench, stool, bed) | The furniture the player last pointed at in VR (within 20 s), else the nearest free one of that kind in the cell, found by name. The NPC walks there, sits or lies down, and sits back down if their AI makes them get up, for up to 3 minutes |
| `StandUp` | Ends a sit or stay order and gets the NPC out of the furniture |
| `GoThere` | Walks to the last non-person thing the player pointed at and stays near it for up to 3 minutes (a pointed seat means Sit). People: use upstream `MoveTo` |
| `OrderDrinks` (bartender, drink: mead/ale/wine, payer: player/npc) | Walks to the bartender (named, else the person the player last pointed at), buys two of the drink at its base value each, walks back and hands one to the player. The drink is the bartender's own, else the kind of a bottle standing in the cell. If the payer cannot afford it, the NPC comes back empty-handed. Queues a `purchase` perception record: the buyer, the bartender and witnesses remember the round (world event type `trade`, so it never counts as drinking) |

| `Undress` | The NPC's own choice, never an order. The game unequips every worn armor and clothing item (weapons stay, nothing leaves their inventory) and remembers what it took off. If their AI puts something back on, it is taken off again, for up to 10 minutes, until `Dress`, combat or a new undress. Queues an attire record with `via=action` and witnesses. Children are refused in game and ignored by the server |
| `Dress` | Puts back on what `Undress` took off (else the NPC's AI picks their outfit) and queues an attire record with `via=action` |

Undress consent: the description and basic-mode prompt tell the NPC to decide in character from their
personality, relationships and memories, mood, and the situation (privacy, who else is nearby, safety).
Being asked, flattered, given gifts or offered gold is only weighed, never an obligation, and drink never
makes them more willing. Nothing in code makes them agree; refusing is not calling the action.

When there is nothing to sit on or nowhere pointed at, the game adds an in-game event line so the NPC can say so.

## Reputation and gossip (S1: word gets around Riverwood)

On with `MANTELLA_LIVING_WORLD=1`. Switch gossip off with `set MANTELLA_LW_GOSSIP=0` (claims and opinions
are still recorded). No AI call is made for any of it except one more field in the existing extraction call.

| Part | Where | What it does |
| --- | --- | --- |
| Seed | `social/data/riverwood.json`, `social/seed.py` | Riverwood's households, kin, friendships and rivalries (Hod and Gerdur married; Sven and Faendal rivals over Camilla), village membership and how gossipy each person is. Matched by Skyrim.esm base form ID, applied once each person has been seen. Edit the JSON freely |
| Claims | `social/claims.py`, table `claim` | Something people can pass on: "Torvar grabbed Gerdur by the left forearm". Made from perceived events (grabs, drinking, buying a round); each person who saw it holds a copy (`knowledge.claim_id`, `believed`) |
| Event processor | `social/processor.py`, table `event_cursor` | Reads `world_event` once per consumer, so each event is appraised once; later goals, player knowledge and body language plug in here |
| Gossip | `social/gossip.py` | When a conversation starts, the village is caught up from its last `sim_tick` in quarter-day steps (at most 60 steps, 150 ms budget). Household and close friends talk most, rivals hardly at all; gossipy people pass news on more; old news fades after 7 days and stops after 30. Each telling is a `gossip_exchange` event, the listener gets their own copy with how much they believe it (trust in the teller, never above 0.9), and their view of the person it is about moves |
| Reports | `memory/extraction.py` | The extraction call also returns "reports": someone telling the NPC what a person did ("I helped Gerdur fix the mill wheel"). Each must cite the line and speaker, like facts; it becomes a claim the NPC can repeat |
| Prompt | `memory/recall.py` | Up to 3 things the NPC was told, with who told them and how sure they are ("I'm not sure I believe it. I did not see it myself."), and a line on how they feel about the player from their own relationship values (`v_relationship`, now with respect, admiration and suspicion) |
| Children | `store/minors.py`, `npc.is_child` | Child NPCs are left out of everything Living World works out: no grabs, clothing, drinking, witnesses, memories, seed, gossip or reputation. Recognised by Mantella's character list (child voices), a child race or voice type, and on the game side by `Actor.IsChild()` |

## Reputation layers and player knowledge (S2)

On with `MANTELLA_LIVING_WORLD=1`. Switch it off with `set MANTELLA_LW_REPUTATION=0` (S1 gossip keeps
working). Hints for the player: `set MANTELLA_LW_INFO_LEVEL=minimal|balanced|informative` (default balanced).
No AI call. Migration `009_reputation.sql`.

| Part | Where | What it does |
| --- | --- | --- |
| New game events | `perception/records.py` (`gift`, `theft`, `hit`), `perception/formation.py` | The player handing an NPC something outside trading, a theft or picked pocket the game itself counted, and the player hitting an NPC who was not an enemy. Each gives the receiver/victim and onlookers a memory and a change of view, and becomes a claim (`gifted`, `stole`, `pickpocketed`, `hit`). A picked pocket goes unnoticed by the victim and is only seen from close by. A theft victim only knows if they saw it |
| Standing by domain | `social/reputation.py`, `standing_change` / `v_standing` | One person's view of another in generosity, honesty, reliability, gentleness, decency, lawfulness and standing (positive = the good end; `trade` is kept for the civic systems). Moved by what they saw (`claims.appraise`) and, less, by what they heard and believed (`claims.hear`), scaled by how much they care about that area (traits) and how close they are to the victim |
| Opinions | `belief` / `v_belief` | Formed when a standing passes 0.2 ("Torvar is generous"; "Torvar is a thief" past 0.5), dropped when it falls back under 0.1. NPC prompts show up to 3 about the player and the people present: "What I've come to think: Torvar is generous (I've seen it myself)." |
| Community and faction views | `v_community_reputation`, `v_faction_reputation`, `community_stance` | Computed when read, never stored: the influence-weighted average of members' own views per domain, plus `regard` from `v_relationship`, and `reach` (the share of the place that has any view). Each guard org in `guard/data/orgs.json` becomes a faction whose members are the guards met in play; guards weigh lawfulness 2.5x and generosity 0.5x. Skyrim's own bounty stays the game's business |
| Retelling | `social/gossip.py` | Each retelling may grow (likelier from dishonest or drunk tellers) or, from honest people, shrink a little (`gossip_exchange` payload `distorted`). A witness who meets someone holding a blown-up version sets it straight (`gossip_correction`): the listener's copy goes back to the true size and the extra they made of it is taken back |
| Traits | `social/traits.py` | Seeded once per person from their Mantella bio by word rules (gossipy, honest, trusting, loyal, bold, greedy, temper, law, family, wealth, honor, modesty, warmth, reserve). A seed file entry always wins. Read: gossipy, honest, trusting, loyal and the values |
| Player hints | `social/hints.py`, `player_notice` | Balanced: at most one hint per conversation start, only about the player or someone they have talked to 3+ times, as hearsay naming who was talking, so the player knows whom to ask ("You overhear Hilde talking about Hod."). Informative: up to 3 with names ("Hod has heard that you helped Gerdur."), plus what the place makes of you in the first conversation after a load. Never about something the player saw; each claim once; shown as notifications with the first NPC line (`mantella_lw_hints`). What the player overhears is stored as their own `knowledge` (`overheard`), which no NPC reads |
| Game side | Mantella-Spell `MantellaRepository.psc` (LIVINGWORLD PATCH: reputation) | Gift (`OnItemRemoved` to an actor, not in barter or container menus, merged per person for 10 s), theft (`OnTrackedStatsEvent` "Items Stolen"/"Pockets Picked" paired with the last `OnItemAdded`), hit ("Assaults" count, or a conversation NPC's `OnHit`; once per person per 15 s) |

## Romance between NPCs (R1: off screen)

Off unless you set `MANTELLA_LW_ROMANCE=1` (on top of `MANTELLA_LIVING_WORLD=1`). No AI call and no animation:
everything is decided on the server and recorded as having happened off screen (`encounter.mode = 'offscreen'`).

| Part | Where | What it does |
| --- | --- | --- |
| Tables | `store/migrations/005_romance.sql` | `romance_bond_change` + `v_romance_bond` (what a pair agreed: courting, dating, partners, betrothed, married, affair, ended), `place`, `encounter`, `consent_decision`; `v_relationship` gains intimacy, commitment, satisfaction and jealousy. All on the save timeline, so loading an older save rolls romance back |
| Seed | `social/data/riverwood.json` (`romance`, `places`) | Romance traits per person (romantic, drive, monogamy, jealous, flirtatious, shy, modesty, attracted_to, intimacy_boundary) and the places people can be alone. Hod is curious and flirts; Gerdur keeps to him less firmly and his flirting hurts her, so she is the likeliest to stray. Seeded spouses start out married |
| Simulation | `social/romance.py` | Caught up with gossip in quarter-day steps. Attraction drifts toward a fixed per-pair "spark" (most pairs have none); people with strong private interest ask to court, rarely ask for an affair; a no gets a cooldown and two nos end the asking; bonded pairs spend evenings and, at night, private time in the most private free place; taken people may flirt where others see; jealousy and unhappiness can end a bond; exes with closeness left may reconcile. Paced slowly: at most one new courtship or affair a week in a village, one bond change per person every 3 days, one night together per pair per day |
| Consent | `social/consent.py` | Each person's own yes, asked separately at invite, arrival and start, logged with its reasons. Hard limits first (child, the person's own boundary for the bond stage, fear, anger, grief, very low mood, heavily drunk, not private enough), then their own wish against their own threshold. Never reads gifts, gold, debts, rank, the other's wish or the player. Drink never raises a yes. Saying no costs the person who says it nothing |
| Consequences | `social/claims.py`, `romance.on_learned` | Romance events become claims (flirted with, spent the night with, broke up with, married...). Inn staff and housemates may notice; private claims spread mostly between close people. A partner who sees or hears of a flirt or a night elsewhere gets jealous, less satisfied and (for a night) trusts less. Encounters never raise attraction or trust |
| Prompt | `memory/recall.py` | The NPC's own love life in their words ("Hod is my spouse, but I've been jealous lately", "I've been secretly seeing Faendal...") |

Everything is on the save timeline: loading an older save takes back the gossip that happened after it.

## Hygiene (H1: who is clean, who is grimy, who notices)

On with `MANTELLA_LIVING_WORLD=1`. Switch it off with `set MANTELLA_LW_HYGIENE=0`. Plan:
`hygiene/` and the "NPC Bathing & Hygiene" plan doc.

- Cleanliness is replayed from `hygiene_change` (migration 006) on the save timeline: dirt builds up at a
  daily rate from each person's work, fights add dirt and blood, swimming washes some off, a bath clears it.
- Each NPC has their own habits (`hygiene/profile.py`): how clean they want to be, how often they bathe,
  when in the day. Defaults come from their work and a per-person draw seeded with their form id; sex is
  never an input. Set any of `hygiene_standard`, `bath_interval_days`, `work_dirt_rate`, `bath_window` in
  `persona_version.traits_json` to override.
- NPCs bathe off screen for now: when someone comes up in conversation, the baths they would have taken
  since are written in at their usual times. Visible bathing near the player is milestone H2.
- The game sends a `fight` record for the player and each adult ally once a fight ends (lowest health
  share), and a `swim` record when the player stops swimming. No other game work.
- Prompt lines: the NPC's own state (grimy, bloodied, freshly bathed), and what they notice about the
  people in the conversation, once per state (`hygiene_notice`), coloured by their own standards.
  No LLM calls.

Test command: say or type "debug max dirt" to an NPC to make yourself filthy and bloodied ("debug max dirt
on Hod" for an NPC in the conversation), and "debug clean" to bathe at once. Off with
`MANTELLA_LW_HYGIENE_DEBUG=0`.

What someone with no clothes on looks like depends on the body mods: `set MANTELLA_LW_UNDRESSED_LOOK=`
`underwear` (vanilla bodies), `female_nude` (CBBE / BHUNP; the default) or `nude` (also SOS / TNG). The game
now sends each person's sex on attire records.

Migrations are now applied by version number present, not by "above the highest", so feature branches
(005 romance, 006 hygiene) can merge in either order.

## Misheard speech

On with `MANTELLA_LIVING_WORLD=1`. Switch off with `set MANTELLA_LW_HEARING=0`. No extra AI call.
Code: `dialogue/hearing.py`.

| Part | What it does |
| --- | --- |
| Rule | Each prompt tells the NPCs the player's words can arrive garbled. A line that makes no sense gets an in-character check ("Thane, you say?") or a request to say it again, not a literal answer. A clear line with one wrong word is just answered. |
| Note | Each player line is checked against a list of commonly misheard Skyrim words ("stain of white run" -> "thane of Whiterun", "Kai's" -> "Caius", "yarl" -> "Jarl") and against near misses of the names of the people present ("Gerdar" -> "Gerdur"). Hits are added before the line as a narration note. The player's words are never rewritten. Add words to `MISHEARD`. |

## Trips: "let's go to the farm" actually sets off

On with `MANTELLA_LIVING_WORLD=1`. Switch off with `set MANTELLA_LW_TRIPS=0`. No extra AI call, no Papyrus change
(it uses Mantella's own `LeadTo` / `TravelTo`). Code: `dialogue/trips.py`, place list `dialogue/travel_places.json`
(copied from Mantella-Spell `MantellaLocationUtils.psc`).

| Part | What it does |
| --- | --- |
| Watch | When an NPC line agrees to set off ("let's head to Battle-Born Farm", "follow me", "I'm right behind you") and the reply did not call LeadTo / TravelTo / Follow itself, the place is taken from that line or from what the player asked for in the last few lines. Names are matched loosely ("Brees home" -> Breezehome); the place where the conversation is happening is skipped. |
| Ask | The game shows "Go with Alfhild to Battle-Born Farm? Say yes or no." When the name could be several places, it shows numbered choices ("say one or two"). Nothing moves before the player answers. |
| Go | "Yes", "one", "two" or the place's name puts `LeadTo` (the NPC walks, the player follows) on the NPC's next line, with a note so the NPC speaks as if setting off. If LeadTo is in Disabled Actions, `TravelTo` with `leave_now`. "No" drops it for the rest of the conversation; an ignored question lapses after two lines. |

## Trade: a deal agreed in conversation actually happens

"I'll give you 100 gold for those two amethysts" used to go nowhere with anyone who is not a merchant:
Mantella's Barter only opens for shopkeepers (Skyrim ignores it on a guard), and Inventory only opens a
window. The `Trade` action (`actions/trade.json`) names what each side hands over; `actions/trade.py`
tidies the AI's wording ("two amethysts", "100 septims") into counts, gold and name spellings, and the
game (Mantella-Spell `LwTrade`) checks the player really has the goods and gold, then swaps everything at
once and tells the NPC "Trade done: ...", or why it did not happen. An NPC short of coin pays the rest
from their savings (added). Barter on a non-merchant now opens their pack instead and says so.

On with `MANTELLA_LIVING_WORLD=1`. Switch off with `set MANTELLA_LW_TRADE=0`.

## Leads: ask about someone without saying their name

On with `MANTELLA_LIVING_WORLD=1`. Switch off with `set MANTELLA_LW_LEADS=0`. No extra AI call.
Code: `dialogue/leads.py`, table `player_lead` (migration 011).

| Part | What it does |
| --- | --- |
| Learning | When the player asks after someone ("Do you know where vault is?"), a name in the NPC's answer that sounds like one of the player's words ("Vald? He owes Maven...") becomes a lead, spelled the way the NPC spelled it. Names the misheard list fixes in a question ("named Wald") count too, and so does a name said right that the NPC repeats. Kind (person, place, thing) comes from the name and the words around it. |
| Phrases | "the man/woman/one I'm looking for" (newest person), "the place I'm looking for" (newest place), "what I'm after" / "my lead" (newest of any kind). The line carries a note: `When Torvar says "the man I'm looking for", they mean Vald, ...`. |
| Hearing | In a question, the newest leads are matched more loosely than other names ("Where's vault hiding?" -> "Vald"). |
| Saves | Rows carry the timeline: loading an older save forgets leads learned after it. The newest five are kept in play. |


Both are on with `MANTELLA_LIVING_WORLD=1`. Switch either off with `set MANTELLA_LW_MOOD_METER=0` or
`set MANTELLA_LW_QUIET_CHATTER=0`.

| Part | Where | What it does |
| --- | --- | --- |
| Mood tag | `mood.py` | Each NPC's prompt asks them to open every reply with `<mood wary 4>`: a feeling word and 0 (furious, hostile) to 5 (calm) to 10 (delighted). The tag is cut from the streamed reply before it is split into sentences, so it is never voiced, subtitled or kept in the history. No extra AI call |
| Storage | `emotion_state` | The latest mood per NPC. A conversation within half a game day starts from it ("still feeling annoyed"); a mood stored after the loaded save's time is ignored |
| Meter | `mantella_lw_mood` on the NPC's line | When the mood changes, the line carries text such as `Gerdur: wary [====------] tipsy` (drink stage added when not sober). Mantella-Spell shows it as a notification |
| Quiet chatter | `mantella_lw_quiet_chatter` on the start reply | Mantella-Spell turns off vanilla hellos, idle chatter, NPC-to-NPC small talk and comments on the player's actions until the conversation ends (game settings, restored afterwards and on load) |

A mood never changes a relationship or what an NPC is willing to do with their body.

## NPC tasks and shared work (W1: server side)

On with `MANTELLA_LIVING_WORLD=1`. Switch off with `set MANTELLA_LW_TASKS=0`. A separate subsystem
(`work/`): it reaches relationships, memory and gossip only through events, and never touches romance
tables or attraction. No AI call is made for it.

| Part | Where | What it does |
| --- | --- | --- |
| Templates | `work/data/riverwood_work.json` | Real needs per person: Gerdur needs 10 pieces of firewood split and stacked for the mill from Last Seed to Sun's Dawn (Hod and Faendal cannot keep up). Months, working hours, outdoor weather, who she would ask, pay (50 gold), size, deadline (3 days from agreeing), cooldown. Edit freely |
| Store | `work/tasks.py`, migration 007 (`task`, `task_change`, `v_task`) | States available, offered, accepted, in progress, paused, completed, failed, expired; every step is a `task.*` world event and a change row on the save timeline, so an older save rolls tasks back |
| Upkeep | `work/needs.py` | At conversation start: opens a need when the season fits and the giver is free, lapses needs nobody took, expires missed deadlines, pauses outdoor work at night or in rain and resumes it |
| Prompt | `work/prompts.py` | "Work on Gerdur's mind": the need and why, the pay, that the player may say no; or how far the agreed work has got; or how it ended in the last 3 days. Asks only during working hours, in fit weather, someone she trusts enough, and not again within a day of a no |
| Actions | `actions/task_*.json` | AgreeTask (player clearly agreed: records a `promise` and the deadline), CheckTask (she looks it over; only real progress counts), ReleaseTask (player backs out, or she calls it off). Server only; the game needs nothing |
| Progress | `work/progress.py` (`work` consumer of the event processor) | `item_given` events (W2 game side: items handed to the giver) count toward the need; reaching it completes the task |
| Consequences | `work/consequences.py` | Done: trust, affinity, respect, familiarity up for the giver toward the worker (more with admiration when on time, more than asked, in good moods; less after rough handling), a first-person memory, a `favor` obligation, and a `helped` claim that gossip spreads. Let down: trust and respect down, harder each time, half as much for backing out openly, a `let_down` claim. After 2+ let-downs with low reliability, nobody asks again and says why |
| Who wants a word | `mantella_lw_wants_a_word` on the start reply | `Name|plugin|base id;...` of people with work they would offer the player now (read by the game's approach step in W2) |
| Developer tool | `python -m livingworld.work.dev --db <world>.sqlite <command>` | `list`, `templates`, `show`, `explain Gerdur`, `create`, `offer` (forced, next conversation), `give`, `complete`, `fail`, `deadline --in`, `advance --days`, `reset`. Refuses to write while the Mantella server runs |

## VR performance monitor (diagnostics)

Off unless you set `MANTELLA_LW_PERF=1` (works with or without `MANTELLA_LIVING_WORLD=1`). It only watches:
it runs in the server, outside the game, and changes nothing the server or the game does. Every half second
(`MANTELLA_LW_PERF_INTERVAL` to change) it writes one JSON line to
`<save folder>/data/livingworld/perf/perf-<date>-<time>.jsonl`.

| Measured | Where it comes from |
| --- | --- |
| CPU frame interval, GPU time per frame, reprojected and dropped frames | SteamVR's own frame timings, read as a background OpenVR app. Needs `pip install openvr` in the server's Python |
| Main-thread responsiveness | A harmless `WM_NULL` sent to the game window and timed, plus Windows' own "not responding" check. A frozen main thread never answers |
| Game memory, page faults, CPU; free memory | `psutil` (optional) |
| Pending LLM calls (waiting or in flight), background jobs running | Counters in the LLM client and the job worker (`perf/stats.py`) |
| Game requests and the slowest one; perception records by type | Counters in the `/mantella` route and `perceived()` |
| Each NPC hold: HIGGS grab/drop events, repeats ignored, how late script ticks ran | `type=perf` records from the game (Mantella-Spell performance probe, see `perf/game_records.py`); they never reach memories |

When the main thread stops answering it logs one warning when the stall starts and one when it ends.
`python -m livingworld.perf.report <file.jsonl>` compares 30 s before each hold, the hold and 30 s after,
lists every stall, and names the likely source: rendering, main thread (engine, physics), scripting, AI
processing, memory, or a mix. The game sends its hold markers with its next request, so talk to someone
after a test hold.

### Upstream patches

| File | Change |
| --- | --- |
| `src/http/routes/mantella_route.py` | Optional traffic recording (does nothing unless `MANTELLA_RECORD_TRAFFIC` is set); request timing for the performance monitor; the diary read request, answered outside the conversation lock |
| `src/llm/client_base.py` | Counts LLM calls waiting or in flight for the performance monitor |
| `src/actions/function_manager.py` | Also loads `livingworld/actions/*.json` (does nothing unless `MANTELLA_LIVING_WORLD` is set) |
| `tests/conftest.py` | Registers the `baseline_behaviour` test marker |
| `src/output_manager.py` | Hands each turn to the Living World when it is on (group turns), refuses synthesis in the wrong voice, keeps the "generating" flag up across a multi-speaker turn, and cuts mood tags out of the streamed reply |
| `src/conversation/context.py` | `focused_on(character)`: a copy of the context holding only the player and one NPC, for building that NPC's own prompt |
| `src/actions/function_manager.py` | Skips actions marked `living_world` unless `MANTELLA_LIVING_WORLD` is set |
| `src/game_manager.py` | Calls the Living World hooks at conversation start, on each request's context (perception records), each spoken line (plus its mood meter), player input, actor changes and conversation end, adds the quiet-chatter key to the start reply, answers the game's diary read request, and wraps Mantella's memory with the recall (does nothing unless `MANTELLA_LIVING_WORLD` is set) |

## Body language, step 1 (BL1): proximity and gaze

NPCs notice where you stand. The game reacts on its own, at once, from a small "expression profile" the
server sends for each NPC in a conversation (comfort distance, how readily they hold your gaze, how
guarded they get, whether they step back). It reports what you did as `type=presence` records; the server
turns those into short-lived feelings and tells the dialogue model, in neutral words, what the NPC noticed.
Mantella-Spell's `MantellaRepository.psc` holds the game side.

| Piece | File | What it does |
| --- | --- | --- |
| Records | `bodylang/presence.py` | `approach`, `crowd`, `backed_off`, `weapon`, `stare`: only ever about the player and one NPC |
| Feelings | `bodylang/state.py` | irritation, unease, fear, embarrassment (0..1) and a "crowded" count, each fading with a half-life in game minutes, stored in `emotion_state.mood_json` under `body` beside the mood meter |
| Appraisal and profile | `bodylang/presence.py` | Rules only: the same moment moves each NPC differently by traits (`personal_space`, `anxiety`, `temper`, `bold`, `warmth`, `reserve`, `dominance`) and by how they see you |
| Dialogue | `integration.py` | What the NPC noticed goes into the first prompt, or as an in-game event on your next line; each thing is told once |
| Mood clamp | `bodylang/presence.py` | The mood tag the model writes moves at most 2 points a turn, and fresh fear or irritation caps it |
| Loop guards | `bodylang/__init__.py`, `harness/bodysim.py` | NPCs never react to each other's body language; an NPC's own step back never counts as you leaving; cooldowns and a per-minute cap. `tests/livingworld/test_bodylang.py` checks a model of the game's rules settles |

`MANTELLA_LW_BODY_LANGUAGE=0` turns all of it off (the game hears it with the conversation start reply).

### Step 2 (BL2): VR gestures

In VR the game reads your right hand and headset through VRIK (0.8.x) and recognises a few gestures. Each
one goes to the NPC you made it to as a presence record, like the rest:

| Gesture | How the game reads it | What the NPC is told |
| --- | --- | --- |
| `wave` | right hand at head height moving side to side | you waved at them |
| `nod`, `shake` | headset bobbing up and down, or side to side, while you stand still | a silent yes or no (told only within 2 game minutes) |
| `point` | right arm held out still, index finger off the trigger | who you pointed at (`about=`) |
| `beckon` | right hand out in front, fingers curled in (grip) twice | a "come here" gesture; the NPC decides |
| `hands_up` | both hands above your head | a surrender; it calms fear and anger a little |
| `thumbs_up`, `thumbs_down` | grip and trigger held, thumb off the stick and buttons, thumb pointing up or down | a thumbs up or down |

`MANTELLA_LW_GESTURES=0` turns gestures off; `MANTELLA_LW_GESTURE_NOTICES=0` stops the game showing a
notice for each gesture it recognised (shown by default while the thresholds are tuned in game).

## Intimate scenes (OStim)

NPCs know when an OStim scene is running, who is in it and what is happening in it (kissing, sex, spanking
and so on, from OStim's own action types), and remember it afterwards as a scene. While the player is in a
scene with someone, Mantella-Spell counts the player's hands on that partner (HIGGS grabs) and slaps on them
into the scene instead of sending them as grabs and hits: no "pulls away" reaction, no guard, no grab or
assault gossip, no drop in how decent or gentle they think the player is. Grabs and hits outside a scene, or
on anyone who is not the player's partner in it, are handled exactly as before.

| Piece | File | What it does |
| --- | --- | --- |
| Records | `perception/records.py` | `type=scene`: `via=end` once per finished scene, `via=now` in the `mantella_lw_scene` snapshot with every request |
| Memories | `intimacy/scenes.py` | Each NPC in it remembers it as shared ("the grabbing and the slaps were part of it, not violence"); a little intimacy and affinity, never trust or attraction. Bystanders who saw it remember it, and the claim is `caught_with`, so a partner reacts as to an affair. Clothes coming off for it (`attire` with `via=scene`) make no "naked" memories |
| Forced scenes | `intimacy/scenes.py` | A scene an animation pack tags `aggressive` is remembered by the person it was done to as being forced (large loss of trust); bystanders remember an assault (claim `forced`). Stock OStim has no such scenes |
| Dialogue | `integration.py` | While a scene runs, each NPC in it is told what is going on and that touching, slaps and rough play are part of it (and that they can still say if something is too much); bystanders who can see it are told so |
| The player's size | `intimacy/size.py` | When the player is a man with The New Gentleman, the game sends the scale TNG gives his genitals; women in the scene with him notice it (5 inches or less unimpressive, more impressive, and thin under 4.25 around or thick from 5.25) in the scene's prompt and remember it. `MANTELLA_LW_TNG_LENGTH_IN` (default 6.0) and `MANTELLA_LW_TNG_GIRTH_IN` (default 4.75) say what TNG's medium size measures with your BodySlide build |

| Witnesses | Mantella-Spell `LwSceneSample` | Every 4 s while any followed scene runs (the player's or one between NPCs), the adults near it are looked at and the best view each had is kept, so someone who walked in and out still saw it. Uses Papyrus Extender's `FindAllReferencesWithKeyword` (falls back to Mantella's five nearest) |
| Betrayal | `intimacy/scenes.py`, `intimacy/common.py` | A witness whose spouse or partner (seeded spouse, or a romance bond short of an affair) is in the scene with someone else remembers it as a betrayal, loses trust in the partner and liking for the other; live, they are told it is their own partner |
| Climax | `intimacy/climax.py` | `type=climax` from OStim's `ostim_actor_orgasm`, with the scene's acts at that moment: where a man's climax landed (face, chest, inside, body; the way OCum places it, guessed, not read from OCum). Remembered by both, and by bystanders who saw it. Afterwards, for up to 4 game hours or until that person bathes or swims, people around notice it where it shows (the face always; the rest only on someone with nothing on) |
| Arousal | `intimacy/arousal.py` | Rapid taps of the arousal key raise the player's erection with The New Gentleman's own `SOSBend` animation events; one lone tap lets it go down; it fades by itself after 2 minutes. Seen only when the player has nothing on the body: witnesses remember it, and in a conversation each NPC notices it; women also take in his size, and how anyone reacts is their own. Noticing it never makes anyone more willing. `MANTELLA_LW_AROUSAL_KEY` (DirectX scan code, default 76 = NumPad 5, 0 off) |
| Covered eyes | `intimacy/arousal.py`, Mantella-Spell `Tools/LWCoverEyes` | The LWCoverEyes power darkens the player's screen (sound plays on) and sends `LivingWorld_EyesCovered`; NPCs around know the player cannot see but can hear |

`MANTELLA_LW_INTIMACY=0` turns it off (the game hears it with the conversation start reply, and then sends
grabs and hits as before). Children are never part of any of it: not in a scene, not as a witness.

### Caught in the act, decency, wandering eyes

| Piece | File | What it does |
| --- | --- | --- |
| Walking in | `intimacy/confront.py`, `intimacy/scenes.py`, Mantella-Spell `LwConfront` | The start reply lists every couple (`mantella_lw_couples`, "Hod\|Gerdur": spouses and courting, dating, partners or betrothed bonds; vanilla lovers count too in game). A witness with a clear view of their own partner in a scene, within 10 m, confronts them: they join the running conversation, or one starts with them, the couple and the player (when near). Once per scene, at most once every 2 minutes. The witness is told they walked in and do not stay silent; the partner in the scene is told who caught them. How either reacts is their own. Forced scenes are not confrontations |
| Not letting it go | `intimacy/confront.py` | For 4 game days after, the witness's prompt says what they saw; within the first day, with their partner or the other person present, they bring it up |
| Decency | `guard/decency.py`, `actions/decency_fine.json`, Mantella-Spell `LwDecencyTick` | The player has had nothing on the body slot for 5 s outside water, a scene or their own house, no conversation is running, and a guard within 35 m can see them (Papyrus Extender, else every person in the player's cell): the guard runs over (`react` record `via=decency`) and a conversation starts. The first time in two days a warning; after that a 20 gold fine the player can pay on the spot (the guard's PayFine: the game moves the gold, as much as the player has, `mantella_lw_fine_gold`), or, refused, ReportCrime. A thane of the guard's hold is asked with deference ('my Thane') and never fined. At most once every 2 minutes |
| Wandering eyes | `bodylang/peek.py`, Mantella-Spell `LwBodyPeek` | A sixth field in each woman's body-language profile: how readily her eyes drift to a naked player (attraction, liking, familiarity, her mood, bold or reserved nature; fear, dislike and irritation stop it). The game makes her glance at his groin for about 1.5 s now and then while he is naked, has TNG's bone and is within 6 m, also mid-conversation, and sends `presence` `what=peek` so her next line knows she looked (no feelings change). Never makes anyone more willing |

Off switches: `MANTELLA_LW_CONFRONT=0`, `MANTELLA_LW_DECENCY=0`, `MANTELLA_LW_PEEK=0` (peek is also off with body
language off). Each reaches the game with the conversation start reply.

## Memory when talking again

Each conversation is turned into each NPC's own memories and facts by a background extraction call. Before
this, that call used the spoken-reply settings (`max_tokens` 250 and the `#` stop sequence in
`llm_params`), so its JSON was often cut off, and memories written as plain sentences were thrown away:
across 127 conversations NPCs kept 8 memories of what was said. Recall also showed only the 8 newest
memories, so a run of grabs pushed every memory of what was said out of the prompt.

| Piece | File | What it does |
| --- | --- | --- |
| Reply length | `src/llm/client_base.py` (`background_request_call`), `integration.py` | Extraction gets its own `max_tokens` (`MANTELLA_LW_EXTRACT_MAX_TOKENS`, default 1500), low temperature, JSON mode (asked again without it if the service refuses), no stop sequence, and does not wait on the dialogue lock, so it never delays an NPC's reply |
| Mending | `memory/extraction.py` | A cut-off or sloppy reply keeps every complete entry; memories as plain sentences are kept; an unusable reply is asked for once more straight away; a fact given the wrong line number is matched to the line by that speaker that really holds it (nothing unsaid is accepted); memories come first in the reply and are asked to be specific |
| Recall | `memory/recall.py` | Six slots for memories of what was said and six for things seen or done, by importance and recency; the same thing repeated is shown once with a count ("18 times, most recently yesterday") |
| Recap | `memory/recall.py` | When the player talked to this NPC in the last 2 game days, the last 8 lines of that conversation go into the prompt word for word, so talking again picks up where it stopped, even before extraction has run |
| Backfill | `memory/backfill.py` | Past conversations (in the current timeline, up to 60) that left no memory of what was said are extracted once more at start-up, each only once. They add memories and facts, never gossip, and never replace a fact learned later. `MANTELLA_LW_MEMORY_BACKFILL=0` skips it |

`MANTELLA_LW_MEMORY_BOOST=0` turns the reply-length, retry, line-matching, recall and recap changes off. The
reply mending stays on: it only keeps what the old code threw away.

## Fog: names and places unknown until told

On with `MANTELLA_LIVING_WORLD=1` and the matching Mantella-Spell build (`LivingWorldFog.psc`). Switch off with
`set MANTELLA_LW_FOG=0` (the game then shows every name again). No extra AI call. Code in `livingworld/fog/`,
tables in migration 012 (`name_known`, `place_known`, `npc.stranger_label`), all on the save timeline.

| Part | Where | What it does |
|---|---|---|
| Strangers | game (`LivingWorldFog.psc`) | A named adult NPC the player has not met shows as "Nord woman" over the crosshair and in subtitles (`SetDisplayName`). Generic NPCs and children never change. Everything sent to the server uses the real name, so memory and actions still work |
| Learning names | `fog/names.py`, `fog/service.py` | The player learns a name when the NPC says it in a line, when someone in the conversation names a person who is there, or when the player says it. An NPC learns the player's name when the player introduces themselves ("my name is", "call me Wolf": an alias is kept as what that NPC calls them), when someone present who knows it says it, or through gossip about the player |
| Prompts | `integration.py` | Each NPC is told whether the player knows their name (introduce yourself when it fits) and whether they know the player's (never say it until told). Names learned mid-conversation reach the model as notes with the player's next line. Memories and Mantella summaries read "the stranger" for an NPC who was never told the player's name |
| Hints | `social/hints.py`, `social/claims.py` | Overheard hints call people the player has not met "a Nord woman"; what an NPC heard about the player calls them "a stranger" until they know the name |
| Places | `fog/places.py`, `fog/data/map_markers.json` | A place an NPC names gets its map marker added for the player (not visited), once per timeline. The list comes from the game's own masters via `fog/extract_map_markers.py`; names shared by several markers are left out |
| Existing saves | migration 012 | Everyone the player already talked to through Mantella counts as met, both ways |

An older game build sends no descriptions, and then nothing changes: no prompts about names, no hidden names.

## Diaries

People the player spends time with keep a private diary the player can find and read in the game. On with
`MANTELLA_LIVING_WORLD=1`; switch off with `set MANTELLA_LW_DIARY=0`. The book itself is the LWDiary companion
plugin (Mantella-Spell `Tools/LWDiary`) plus two small hooks in `MantellaConversation.psc`. Code in
`livingworld/diary/`, tables in migration 013 (`diary_entry`, `diary_read`), all on the save timeline.

| Part | Where | What it does |
|---|---|---|
| Who | `diary/service.py` | Any adult NPC who was in at least `MANTELLA_LW_DIARY_MIN_TALKS` (default 5) conversations with the player. Children never. The start reply lists the participants who keep one (`mantella_lw_diary_ref_ids`, `_npc_ids`, `_titles`); the game gives each a book "Gerdur's Diary" (with fog on and the name unknown: "Nord woman's Diary") |
| Writing | `diary/service.py`, job `diary_entry` | After a conversation, each keeper in it (re)writes that day's entry with the background model, from their own memories of the day, what they were told, and how they feel about the player. The first entry looks back over the most important memories so far. A day is rewritten only when there are new memories, at most `MANTELLA_LW_DIARY_REWRITES` (default 3) times. One short call (JSON reply) per entry |
| Prompt | `integration.py` | A keeper knows they keep a diary and would be upset if it were read |
| Reading | `src/http/routes/mantella_route.py` | The game's request `mantella_lw_diary_read` (outside any conversation, not behind the conversation lock) gets the newest six days as pages, newest first. It never answers with an error reply |
| Caught | `diary/service.py` | If the owner saw the player reading it: a memory ("I caught Torvar reading my diary"), trust -0.25, liking -0.15, suspicion +0.25. Others the game saw watching: a memory and a little suspicion. Stored as a `diary.read` world event |
