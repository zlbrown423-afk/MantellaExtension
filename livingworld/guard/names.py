"""Generic guards show their own name in game ("Whiterun Guard" becomes "Hrolf").

``profiles.py`` already gives each generic guard the player meets a stable personal name, stored in
``guard_profile`` (not on the timeline: a name, like a voice, stays with the person) and keyed by the
guard's own npc row, which is keyed by its reference ID. Until now only the dialogue model knew it. This
module decides when the game may show it:

- With the fog on (``livingworld/fog``), once the player has learned it: the guard says it, another person
  in the conversation says it, or the player calls the guard by it. That is stored in ``name_known`` like
  any other learned name, so it rides the save timeline and an older save forgets it again.
- With the fog off (or an older game build), as soon as the guard has a name.

Nothing about who the guard is changes. The game keeps sending the real name ("Whiterun Guard") to
Mantella (``LivingWorldFog.RealName`` on the game side), so the npc row, the voice binding, Mantella's
character lookup, summaries and memories all stay where they were. Only what the player sees changes:
the name over the crosshair, subtitles and notices.

On with the guard careers unless ``MANTELLA_LW_GUARD_NAMES=0``. Switched off, the game puts the shared
name back on every guard it renamed.
"""
from __future__ import annotations

import os
from typing import Iterable

from livingworld.fog import names as N, service as fog_names
from livingworld.guard import profiles

ENV = "MANTELLA_LW_GUARD_NAMES"
_OFF = {"0", "false", "no", "off"}

# Sent with the conversation start reply
KEY_ON = "mantella_lw_guard_names_on"        # bool: show generic guards by their own names
# Sent with the start reply and with NPC lines: guards to show by name from now on
KEY_IDS = "mantella_lw_guard_name_ids"       # int[]: each guard's reference form ID, exactly as the game sent it
KEY_NAMES = "mantella_lw_guard_names"        # string[]: the name to show for each


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def given_name(db, npc_id: int) -> str | None:
    p = profiles.get(db, npc_id)
    return p.given_name if p is not None else None


def player_knows(db, player_id: int, npc_id: int, name: str) -> bool:
    """Whether the player has learned this guard's own name on the current timeline (any time, not only the
    latest name stored for the pair: a line saying 'Whiterun guard' must not hide it again)."""
    return db.execute(
        "SELECT 1 FROM name_known k JOIN v_timeline_scope s ON s.timeline_id = k.timeline_id AND k.game_time <= s.cutoff_gt"
        " WHERE k.knower_id = ? AND k.subject_id = ? AND k.name = ? LIMIT 1", (player_id, npc_id, name)).fetchone() is not None


def shown(db, npc_id: int, player_id: int | None, fog_on: bool) -> str | None:
    """The name the game should show for this guard now, or None to leave the shared one."""
    name = given_name(db, npc_id)
    if not name:
        return None
    if not fog_on:
        return name
    return name if player_id is not None and player_knows(db, player_id, npc_id, name) else None


def npc_line(db, speaker_id: int, text: str, present: Iterable[int], player_id: int, game_time: float,
             conv_id: int | None) -> list[str]:
    """Guards whose own name was said in an NPC line (their own, or by someone else here): the player learns
    it. Returns log lines. Case-sensitive, as the model spells names right."""
    return _heard(db, speaker_id, text, present, player_id, game_time, conv_id, ignore_case=False)


def player_line(db, text: str, present: Iterable[int], player_id: int, game_time: float,
                conv_id: int | None) -> list[str]:
    """The player calling a guard here by their own name: they clearly know it. Speech-to-text often
    lowercases names, so case is ignored."""
    return _heard(db, player_id, text, present, player_id, game_time, conv_id, ignore_case=True)


def _heard(db, speaker_id: int, text: str, present: Iterable[int], player_id: int, game_time: float,
           conv_id: int | None, ignore_case: bool) -> list[str]:
    out: list[str] = []
    if not text:
        return out
    for npc_id in present:
        if npc_id == player_id:
            continue
        name = given_name(db, npc_id)
        if not name or player_knows(db, player_id, npc_id, name) or not N.says_name(text, name, ignore_case):
            continue
        if speaker_id == npc_id:
            how, by = "introduced", None
        elif speaker_id == player_id:
            how, by = "said", None
        else:
            how, by = "named", speaker_id
        fog_names.learn(db, player_id, npc_id, name, how, game_time, by, conv_id)
        out.append(f"the player learned the guard {name}'s name ({how})")
    return out
