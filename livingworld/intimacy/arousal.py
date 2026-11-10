"""The player's erection (The New Gentleman) and the player covering their eyes, as the NPCs around see them.

Arousal: in game, rapid taps of the arousal key raise the player's erection with TNG's own animation events;
one lone tap lets it go down, and it goes down by itself after two minutes without a tap. When it crosses
"visibly aroused" the game sends an ``arousal`` record. It only shows when the player has nothing on the body
(TNG hides it under clothes), so:

* each adult who can see a naked, aroused player remembers it; women also take in his size (``size.py``);
* while it lasts, the player's attire snapshot carries ``erect=1`` and everyone in the conversation or able to
  see him notices it. A woman reacts in her own way (flustered, amused, put off, curious, interested),
  from who she is and how she feels about him; a man too. Noticing it never makes anyone more willing.
  Partners in a scene with him already know (``scenes.py``) and get nothing extra.

Covered eyes: the LWCoverEyes power darkens the player's screen and sends an ``eyes`` record; while covered the
attire snapshot carries ``eyes=1`` and the NPCs around know the player cannot see but can hear.

``MANTELLA_LW_AROUSAL_KEY`` sets the key (a DirectX scan code; default 76, NumPad 5; 0 switches it off).
"""
from __future__ import annotations

import os

from src import utils

from livingworld.intimacy import common, size as sizes
from livingworld.perception.formation import MemoryDraft
from livingworld.perception.records import PerceptionRecord

logger = utils.get_logger()

EVENT_AROUSAL = "intimacy.arousal"
EVENT_EYES = "intimacy.eyes"
KEY_AROUSAL_KEY = "mantella_lw_arousal_key"   # start reply: the arousal key's scan code
AROUSAL_KEY_ENV = "MANTELLA_LW_AROUSAL_KEY"
DEFAULT_AROUSAL_KEY = 76                       # NumPad 5
NOT_MORE_WILLING = "Seeing it does not make {name} any more willing to do anything."


def arousal_key() -> int:
    try:
        value = int(os.environ.get(AROUSAL_KEY_ENV, "") or DEFAULT_AROUSAL_KEY)
    except ValueError:
        return DEFAULT_AROUSAL_KEY
    return value if 0 <= value <= 511 else DEFAULT_AROUSAL_KEY


def record_arousal(pipeline, record: PerceptionRecord):
    """Only a naked player becoming visibly aroused makes memories; anything else is not stored."""
    from livingworld.perception.attire import sex_of
    from livingworld.perception.pipeline import Outcome
    if record.via != "erect" or record.cover != "none":
        return Outcome(None)
    existing = common.existing_event(pipeline.store, record.key)
    if existing is not None:
        return Outcome(existing, duplicate=True)
    gt = record.game_time
    player_id = pipeline.identities.ensure_npc(record.actor.identity(), gt)
    seen, unseen = common.seers(pipeline, record, {player_id})
    size = sizes.from_scale(record.player_size)
    who = record.actor.name
    drafts: dict[int, tuple[MemoryDraft, list[int]]] = {}
    with pipeline.store.transaction() as db:
        for npc_id, how in seen.items():
            content = f"{common.hedge(how)} {who} naked and visibly aroused{common.place(record)}."
            if size is not None and sex_of(db, npc_id) == "female" and (how.distance_m or 0.0) <= 4.0:
                content += sizes.memory_line(who, size)
            drafts[npc_id] = (MemoryDraft(content, None, 0.5, -0.05, 0.45, how.confidence, "witnessed", {}), [])
    payload = {"level": record.level, "player_size": record.player_size, "place_name": record.location,
               "nearby_but_unseen": unseen}
    outcome = common.store(pipeline, record, EVENT_AROUSAL, [player_id], payload, {}, seen, drafts)
    logger.log(23, f"Living World: stored {EVENT_AROUSAL} event {outcome.event_id} ({record.key});"
                   f" seen by {len(seen)} NPC(s)")
    return outcome


def record_eyes(pipeline, record: PerceptionRecord):
    """Covering the eyes is a moment, not a memory worth keeping: stored as an event, no memories."""
    from livingworld.perception.pipeline import Outcome
    existing = common.existing_event(pipeline.store, record.key)
    if existing is not None:
        return Outcome(existing, duplicate=True)
    player_id = pipeline.identities.ensure_npc(record.actor.identity(), record.game_time)
    seen, unseen = common.seers(pipeline, record, {player_id})
    payload = {"covered": record.via == "covered", "place_name": record.location, "nearby_but_unseen": unseen}
    return common.store(pipeline, record, EVENT_EYES, [player_id], payload, {}, seen, {})


def lines_for(db, npc_id: int, name: str, present: dict[int, str], attire: dict, in_scene_with_player: bool) -> list[str]:
    """What ``npc_id`` notices about the player's arousal and covered eyes right now. (A woman's take on his
    size when he is naked comes from ``perception/awareness.py`` with ``size.seen_line``.)"""
    from livingworld.perception.attire import sex_of
    lines = []
    for other_id, state in attire.items():
        if other_id == npc_id or not getattr(state, "is_player", False):
            continue
        visible = other_id in present or npc_id in state.seen_by
        if state.eyes_covered and visible:
            lines.append(f"{state.name} has their eyes covered right now and cannot see anything, but can still"
                         f" hear everything that is said.")
        if not (state.erect and not state.covered and visible) or in_scene_with_player:
            continue
        lines.append(f"{name} can see that {state.name} is naked and visibly aroused.")
        if sex_of(db, npc_id) == "female":
            lines.append(f"How {name} reacts is her own: flustered, amused, put off, offended, curious or interested,"
                         f" from who she is and how she feels about {state.name}. She may say so or look away.")
        else:
            lines.append(f"How {name} reacts is his own, from who he is and how he feels about {state.name}.")
        lines.append(NOT_MORE_WILLING.format(name=name))
    return lines
