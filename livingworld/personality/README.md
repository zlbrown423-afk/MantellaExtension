# Personality profiles (adult women)

Who an adult woman is in love and intimacy: her desire, confidence, pace, what draws her, what puts her off and her
limits. Profiles are generated once, change slowly with what happens to her, can be set by hand, and are only
partly known to the player. On by default with Living World; off with `MANTELLA_LW_PERSONALITY=0`.

High desire never means willingness. Consent is decided by `social/consent.py` (NPC to NPC) and touch consent
(with the player), case by case. A profile only feeds those systems her limits and leanings.

## Who gets a profile

Only women whose adulthood is established (`eligibility.py`):

- the game says female, and she is not the player;
- not a child by any rule in `store/minors.py` (child race or voice, Mantella's child list, the game's flag);
- her race is a known adult person race (Nord, Imperial, Breton, Redguard, the elves, Orc, Khajiit, Argonian,
  Elder, including vampire variants).

An unknown race means no profile, unless the settings file says `"adult_confirmed": true` for her. That can
never switch a profile on for anyone marked a child.

## What a profile holds (`schema.py`)

| Attribute | Kind | Tier | Max change over time |
|---|---|---|---|
| libido | 0-100 | private | 8 |
| sexual_confidence | 0-100 | private | 20 |
| initiation (makes the first move) | 0-100 | personal | 15 |
| selectiveness | 0-100 | personal | 20 |
| privacy | 0-100 | personal | 10 |
| romantic_inclination | 0-100 | public | 15 |
| intimacy_comfort | 0-100 | private | 20 |
| exclusivity (faithfulness) | 0-100 | private | 10 |
| social_confidence | 0-100 | public | 15 |
| openness (about her wants) | 0-100 | personal | 20 |
| relationship_style | romantic / balanced / casual | public | - |
| intimacy_pace | slow / moderate / quick | personal | - |
| relationship_dynamic | leads / equal / follows | personal | - |
| intimacy_boundary | never / married_only / committed_only / after_courtship / casual_ok | boundary | - |
| attracted_to_sex | list: male, female | private | - |
| attracted_to | list: kindness, courage, strength, wit, humour, confidence, ambition, loyalty, gentleness, intelligence, piety, wealth, honesty, mystery, magic, hard_work | personal | - |
| dislikes | list: arrogance, cruelty, drunkenness, dishonesty, crude_talk, pushiness, cowardice, poor_hygiene, jealousy, recklessness, greed, lawbreaking | personal | - |
| boundaries | list: no_public_affection, no_unasked_touch, no_drunk_advances, not_with_strangers, not_with_taken, no_crude_talk, no_rough_handling | boundary | only added, never removed |

Choices and lists don't change over time, with one exception: the `no_unasked_touch` boundary can be added.
Temporary states (mood, arousal, drink) stay in `mood.py`, `intimacy/arousal.py` and `perception/intoxication.py`.

## Modes and precedence (`profile.py`)

- **automatic**: everything is generated (`generate.py`), and everything may change over time.
- **hybrid** (the default): generated, but any value you set by hand replaces the generated one and is locked.
- **manual**: only the values you set. Nothing is generated and nothing changes over time.

Strongest first:

1. Off switches: the module off, `"enabled": false` for her, or not eligible.
2. Values set by hand in the settings file.
3. Change over time, on top of
4. the generated starting point, shifted by the file's `baseline`.

The AI never writes a profile value. Prompts tell it to stay true to the stored profile and never invent
preferences.

## Generation (`generate.py`)

There is no LLM call and no cost. Inputs are, in order:

1. the Riverwood romance seed (drive, romantic, shy, flirtatious, monogamy, intimacy_boundary, attracted_to,
   mapped one to one, so the romance simulation sees exactly the numbers it had before);
2. her traits from `social/traits.py`;
3. work and standing words in her name and bio;
4. marriage;
5. her hygiene standard;
6. a draw seeded by her form id.

She gets the same profile in every save and every run. It is stored on first sight and never re-rolled: a bio
change needs `regenerate`.

## Change over time (`evolve.py`)

This runs at the start and end of each conversation she is in. It reads what other systems already recorded
since her last checkpoint:

- her relationship changes (trust and closeness gained or lost);
- unwanted handling (grabs that cost trust);
- romance bond changes (committed, breakup);
- intimate encounters and how satisfied she was.

Each attribute moves at most 3 points per game day and never further than its drift limit. Every change is
stored with its reason and cause in `personality_change`, on the save timeline, so loading an older save rolls
her back like everything else.

## Discovery (`discovery.py`)

The player starts knowing nothing. What she will talk about depends on her trust, affinity, closeness and
familiarity toward the player, her openness and privacy, and whether others are listening:

- **public**: always;
- **personal**: once she is fairly comfortable with the player;
- **private**: when she trusts the player and feels close, and not in company if she is a private person;
- **boundary**: always (she states limits plainly).

Items listed under `"private"` in the settings file are never shared.

The player's knowledge (`personality_knowledge`) rises only from evidence:

- **known**: she said it, while it was hers to share;
- **disclosed**: she stated a boundary;
- **suspected**: the player saw her flirt, or holds gossip that she flirted or spent a night with someone.

A stated boundary is a limit, never a yes to anything else.

## Prompts (`prompt.py`)

- **Every prompt for her** gets a short block: how she is in love (public tier) and, if she is drawn to the
  player, whether she makes the first move or waits to be asked. Nothing private.
- **When the player's line turns to romance, intimacy, touch or her preferences**, a note goes on that line:
  - how she is (pace, who leads, what draws her, what puts her off) as behaviour guidance;
  - her private side, only if she would share it now, and otherwise just that she keeps it private;
  - her limits in full;
  - what the player already knows of her.
- **Every block ends with the same rule**: never invent preferences; desire is never a yes; boundaries hold.

## The settings file

The file is `<Mantella save folder>/data/livingworld/personality.json`, shared by every world. Set
`MANTELLA_LW_PERSONALITY_FILE` to use another one. It is read again whenever it changes, so there is no restart.

Example: `data/personality.example.json` (Aela leads and is bold, Ysolda is romantic and private about desire,
Camilla is selective and wants a courtship, Lydia is fully hand-written in manual mode, Carlotta is automatic).
Schema: `data/personality.schema.json`.

```json
{
  "version": 1,
  "enabled": true,
  "default_mode": "hybrid",
  "evolution": {"enabled": true, "rate": 1.0},
  "baseline": {"initiation": -5},
  "npcs": {
    "Ysolda": {"values": {"libido": 55, "boundaries": ["no_public_affection"]}, "private": ["libido"]},
    "Skyrim.esm|0x0001A6A0": {"enabled": false}
  }
}
```

An NPC is keyed by name (case ignored) or `Plugin.esm|0xFORMID`. The form id form wins when both match.
Bad entries are skipped and logged, and the rest of the file is still used.

## Admin tool

Run it from the Mantella folder. Close Mantella before running reset or regenerate.

```
python -m livingworld.personality --world "...\data\livingworld\TitusGlicc1.sqlite" list
python -m livingworld.personality --world W.sqlite inspect Ysolda     # values, sources, drift, history, player view
python -m livingworld.personality --world W.sqlite history Ysolda
python -m livingworld.personality --world W.sqlite known Ysolda       # only what the player has found out
python -m livingworld.personality --world W.sqlite set Ysolda libido 60
python -m livingworld.personality --world W.sqlite set Ysolda boundaries no_unasked_touch,not_with_taken
python -m livingworld.personality --world W.sqlite mode Ysolda manual
python -m livingworld.personality --world W.sqlite disable Ysolda     (enable Ysolda)
python -m livingworld.personality --world W.sqlite export > profiles.json
python -m livingworld.personality --world W.sqlite import profiles.json [--take values]
python -m livingworld.personality --world W.sqlite reset Ysolda [--clear-manual]
python -m livingworld.personality --world W.sqlite regenerate Ysolda
```

In game, say or type in a conversation:

- "debug profile": writes each woman's full profile to the server log and to
  `data/livingworld/personality-inspect/<name>.json`;
- "debug profile known": the player's view only;
- "debug profile evolve", "debug profile reset", "debug profile regenerate".

Turn these commands off with `MANTELLA_LW_PERSONALITY_DEBUG=0`.

## Integration points

- `integration.py`:
  - `_personality_started` (conversation open: change over time and observations);
  - `recall_text` (the standing block);
  - `personality_notes` (the topic note);
  - `npc_line` (disclosure);
  - `player_line` (debug commands);
  - `_close` (change over time).
- `src/conversation/conversation.py`: one `LIVINGWORLD PATCH` adds `personality_notes` to the player's message.
- `social/romance.py` `traits()` lays the profile over romance traits through `bridge.romance_traits`. Consent
  reads those romance traits.
- Migration `017_personality.sql`:
  - `personality_profile`: the generated starting point, not on the timeline;
  - `personality_change`: on the timeline;
  - `personality_knowledge`: on the timeline;
  - `personality_admin`: the audit log.

## Known limitations

- Disclosure is spotted with word lists on her own line. A paraphrase can be missed. It never over-reports
  anything that wasn't hers to share.
- Topic detection is also by words, and deliberately conservative ("lovely day" is not romance).
- NPCs don't walk up to the player to start a romantic talk yet. That needs a game-side trigger like the guards'
  "wants a word". Inside a conversation, she does take the first move when her profile and attraction say so.
- Men have no profiles in this subsystem. Romance keeps using its own traits for them.
- Hand-set values apply to every world. There are no per-world overrides yet.
- The dialogue personality (vocabulary, toggles, sliders) and the intimacy action preferences with the OStim
  adapter are the next two milestones.
