"""Diaries: people the player spends a lot of time with keep a private diary the player can find and read.

- Who: any adult NPC the player has talked with at least ``MANTELLA_LW_DIARY_MIN_TALKS`` times (default 5).
  Children never keep one (see ``store/minors.py``).
- Writing: after a conversation with a keeper, the background model writes (or rewrites) that day's entry from
  the NPC's own memories, what they heard, and how they feel about the player. Private and honest: things they
  would not say to the player's face. At most ``MANTELLA_LW_DIARY_REWRITES`` versions of one day (default 3),
  and none when nothing new happened since the last version, so it costs very little.
- In the game (LWDiary.esp, Mantella-Spell ``Tools/LWDiary``): a book "<name>'s Diary" in the NPC's pockets
  (pickpocket it) or lying on a table in their home (owned by them, so taking it is theft). Opening it asks
  this server for the pages (request type ``mantella_lw_diary_read``) and shows them as message pages.
- Getting caught: when the owner sees the player reading it, they remember it, trust and like the player less,
  and grow suspicious. Others who see it remember it too.

On with the Living World unless ``MANTELLA_LW_DIARY=0``.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_DIARY"
MIN_TALKS_ENV = "MANTELLA_LW_DIARY_MIN_TALKS"
REWRITES_ENV = "MANTELLA_LW_DIARY_REWRITES"
DEFAULT_MIN_TALKS = 5
DEFAULT_REWRITES = 3
_OFF = {"0", "false", "no", "off"}

JOB = "diary_entry"

# Sent with the conversation start reply (one entry per participant who keeps a diary, same order in each list)
KEY_DIARY_ON = "mantella_lw_diary_on"                 # bool
KEY_DIARY_REF_IDS = "mantella_lw_diary_ref_ids"       # int[]: the keeper's reference form ID (as the game sent it)
KEY_DIARY_NPC_IDS = "mantella_lw_diary_npc_ids"       # int[]: the keeper's id in this server's store
KEY_DIARY_TITLES = "mantella_lw_diary_titles"         # string[]: what the book is called ("Gerdur's Diary")

# The game opening a diary (its own request, outside any conversation)
REQUEST_READ = "mantella_lw_diary_read"
KEY_WORLD_ID = "mantella_lw_diary_world_id"           # string: player name + MCM world id (as a conversation start)
KEY_OWNER_NPC_ID = "mantella_lw_diary_owner"          # int: the npc id the start reply gave for this diary
KEY_SEEN_BY_OWNER = "mantella_lw_diary_seen"          # bool: the owner saw the player reading it
KEY_WITNESS_REF_IDS = "mantella_lw_diary_witness_ref_ids"   # int[]: others who saw it (reference form IDs)
KEY_WITNESS_NAMES = "mantella_lw_diary_witness_names"       # string[]: their names, same order
KEY_LOCATION = "mantella_lw_diary_location"           # string
KEY_GAME_DAYS = "mantella_lw_diary_game_days"         # float: GameDaysPassed
# The reply
REPLY_READ = "mantella_lw_diary"
KEY_PAGES = "mantella_lw_diary_pages"                 # string[]: newest entry first
KEY_TITLE = "mantella_lw_diary_title"                 # string


def diary_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def _int_env(name: str, default: int, lowest: int) -> int:
    try:
        return max(lowest, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def min_talks() -> int:
    return _int_env(MIN_TALKS_ENV, DEFAULT_MIN_TALKS, 1)


def max_rewrites() -> int:
    return _int_env(REWRITES_ENV, DEFAULT_REWRITES, 1)
