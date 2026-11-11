"""Actor payloads shaped like the ones MantellaConversation.psc sends.

Form IDs for Gerdur and Hod are their Skyrim.esm IDs from data/Skyrim/skyrim_characters.csv.
Voice type strings mimic the game's format; their numeric IDs are placeholders.
"""
from __future__ import annotations

from typing import Any

from src.http.communication_constants import communication_constants as comm_consts


def actor(name: str, base_id: int, ref_id: int, gender: int, race: str, voice_type: str,
          is_player: bool = False, relationship_rank: int = 0) -> dict[str, Any]:
    return {
        comm_consts.KEY_ACTOR_BASEID: base_id,
        comm_consts.KEY_ACTOR_REFID: ref_id,
        comm_consts.KEY_ACTOR_NAME: name,
        comm_consts.KEY_ACTOR_GENDER: gender,
        comm_consts.KEY_ACTOR_RACE: f"[Race <{race}Race (00013746)>]",
        comm_consts.KEY_ACTOR_VOICETYPE: f"[VoiceType <{voice_type} (00013AD2)>]",
        comm_consts.KEY_ACTOR_ISPLAYER: is_player,
        comm_consts.KEY_ACTOR_ISENEMY: False,
        comm_consts.KEY_ACTOR_ISINCOMBAT: False,
        comm_consts.KEY_ACTOR_RELATIONSHIPRANK: relationship_rank,
        comm_consts.KEY_ACTOR_CUSTOMVALUES: {},
        comm_consts.KEY_ACTOR_EQUIPMENT: {},
    }


def player(name: str = "Prisoner") -> dict[str, Any]:
    p = actor(name, 0x7, 0x14, 0, "Nord", "MaleEvenToned", is_player=True)
    p[comm_consts.KEY_ACTOR_CUSTOMVALUES] = {
        comm_consts.KEY_ACTOR_PC_DESCRIPTION: "",
        comm_consts.KEY_ACTOR_PC_VOICEPLAYERINPUT: False,
    }
    return p


def gerdur() -> dict[str, Any]:
    return actor("Gerdur", 0x0001347C, 0x00013489, 1, "Nord", "FemaleNord")


def hod() -> dict[str, Any]:
    return actor("Hod", 0x0001347D, 0x0001348A, 0, "Nord", "MaleNord")


def alfhild() -> dict[str, Any]:
    """Alfhild Battle-Born (Whiterun; runs Battle-Born Farm). Form IDs from data/Skyrim/skyrim_characters.csv."""
    return actor("Alfhild Battle-Born", 0x00013BB0, 0x0001A68B, 1, "Nord", "FemaleNord")
