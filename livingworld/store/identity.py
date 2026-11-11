"""Stable NPC identity: one ``npc`` row per actor, keyed by plugin and full form IDs, never by name.

Mantella keys characters by name and keeps only the last 6 hex digits of each form ID, so two
"Whiterun Guard" NPCs share one memory and an NPC from another plugin can collide with a vanilla
one (audit risks R4). Here the full IDs from the game's request are kept and split into the
plugin part and the plugin-relative part.

Until the game side sends plugin file names (Milestone 1, PR D), only load index 00 can be named
for sure (Skyrim.esm). Other actors get a placeholder plugin such as ``load_index:05`` or
``light:012``, which stays stable as long as the load order does.
"""
from __future__ import annotations

from src import utils
from dataclasses import dataclass
from typing import Any

from livingworld.store.world_store import WorldStore

logger = utils.get_logger()

# Optional keys a future Mantella-Spell build can send next to the form IDs (PR D).
KEY_BASE_PLUGIN = "mantella_actor_base_plugin"
KEY_REF_PLUGIN = "mantella_actor_ref_plugin"


@dataclass(frozen=True)
class FormId:
    plugin: str
    local_id: str      # '0x0001347C': plugin-relative, load-order free when plugin is a real name
    mantella_id: str   # the 6-digit form Mantella's own code uses, for matching Characters

    @property
    def full(self) -> str:
        return f"{self.plugin}|{self.local_id}"


def to_hex8(raw: Any) -> str:
    value = int(raw)
    if value < 0:
        value += 2 ** 32
    return f"{value:08X}"


def parse_form_id(raw: Any, plugin_name: str | None = None) -> FormId:
    """Split a runtime form ID from the game into plugin and plugin-relative parts."""
    hex8 = to_hex8(raw)
    if hex8.startswith("FE"):
        slot, local = hex8[2:5], hex8[5:]
        plugin = plugin_name or f"light:{slot}"
        mantella_id = local.rjust(6, "0")
        local_id = "0x" + local.rjust(8, "0")
    else:
        index, local = hex8[:2], hex8[2:]
        plugin = plugin_name or ("Skyrim.esm" if index == "00" else f"load_index:{index}")
        mantella_id = local
        local_id = "0x00" + local
    return FormId(plugin=plugin, local_id=local_id, mantella_id=mantella_id)


@dataclass(frozen=True)
class ActorIdentity:
    """What the game told us about one actor, before Mantella truncated it."""
    name: str
    base: FormId
    ref: FormId
    is_player: bool

    @classmethod
    def from_actor_json(cls, actor_json: dict[str, Any]) -> "ActorIdentity":
        from src.http.communication_constants import communication_constants as comm_consts
        base = parse_form_id(actor_json[comm_consts.KEY_ACTOR_BASEID], actor_json.get(KEY_BASE_PLUGIN))
        ref = parse_form_id(actor_json[comm_consts.KEY_ACTOR_REFID], actor_json.get(KEY_REF_PLUGIN))
        return cls(
            name=str(actor_json[comm_consts.KEY_ACTOR_NAME]),
            base=base,
            ref=ref,
            is_player=bool(actor_json.get(comm_consts.KEY_ACTOR_ISPLAYER, False)),
        )


class VoiceBindingConflict(Exception):
    pass


def game_voice_changed(bound_type: str | None, current_type: str | None) -> bool:
    """True if the game now reports a different voice type than the one stored with a binding.

    Unknown on either side counts as unchanged, so a missing voice type never loosens the lock.
    """
    if not bound_type or not current_type:
        return False
    return bound_type.strip().lower() != current_type.strip().lower()


class IdentityRegistry:
    """Finds or creates ``npc`` rows and binds each NPC's voice the first time it is seen."""

    def __init__(self, store: WorldStore) -> None:
        self.store = store

    def ensure_npc(self, identity: ActorIdentity, game_time: float, race: str | None = None,
                   sex: str | None = None, is_generic: bool = False, voice: str | None = None) -> int:
        from livingworld.store.minors import looks_like_child
        child = (not identity.is_player) and looks_like_child(identity.base.local_id, race, voice)
        with self.store.transaction() as db:
            if identity.is_player:
                row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
                if row:
                    return row[0]
            # the base record's plugin owns the NPC definition
            row = db.execute(
                "SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? AND ref_form_id = ?",
                (identity.base.plugin, identity.base.local_id, identity.ref.full),
            ).fetchone()
            if row:
                if child:
                    db.execute("UPDATE npc SET is_child = 1 WHERE npc_id = ? AND is_child = 0", (row[0],))
                return row[0]
            alias = db.execute(   # another reference of a known NPC (store/rebind.py)
                "SELECT npc_id FROM npc_ref_alias WHERE plugin = ? AND base_form_id = ? AND ref_form_id = ?",
                (identity.base.plugin, identity.base.local_id, identity.ref.full)).fetchone()
            if alias:
                return alias[0]
            cur = db.execute(
                "INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, race, sex, is_player,"
                " is_generic, created_gt, is_child) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identity.base.plugin, identity.base.local_id, identity.ref.full, identity.name, race,
                 sex if sex in ("male", "female") else None, int(identity.is_player), int(is_generic), game_time,
                 int(child)),
            )
            logger.info("Living World: new NPC %s (%s %s) -> npc_id %s",
                        identity.name, identity.base.plugin, identity.base.local_id, cur.lastrowid)
            return cur.lastrowid

    def bind_voice(self, npc_id: int, tts_service: str, voice_model: str, game_voice_type: str | None,
                   game_time: float) -> str:
        """Bind a voice on first sight and return the bound model.

        A locked binding is never changed by Mantella picking another voice model: if Mantella
        later asks for a different voice for the same NPC while the game still reports the same
        voice type, the binding wins and ``VoiceBindingConflict`` is raised so the caller can log it.

        The one exception is when the game itself now gives the actor a different voice type than
        the one stored with the binding. Leveled actors such as town guards are rebuilt from a
        leveled list when their cell resets, and the same reference can come back with another
        voice type (MaleGuard one day, MaleNordCommander the next). Named NPCs keep the voice type
        of their base record, so this never fires for them. When it happens the binding follows
        the game and is replaced (also when the model stays the same, so the stored voice type is current).
        """
        with self.store.transaction() as db:
            row = db.execute("SELECT voice_model, game_voice_type FROM voice_binding WHERE npc_id = ?",
                             (npc_id,)).fetchone()
            if row is not None and game_voice_changed(row[1], game_voice_type):
                db.execute("DELETE FROM voice_binding WHERE npc_id = ?", (npc_id,))
                logger.info("Living World: npc_id %s now has game voice type %s (was %s); voice rebound "
                            "from '%s' to '%s'", npc_id, game_voice_type, row[1], row[0], voice_model)
                row = None
            if row is None:
                db.execute(
                    "INSERT INTO voice_binding (npc_id, tts_service, voice_model, game_voice_type, bound_gt)"
                    " VALUES (?,?,?,?,?)",
                    (npc_id, tts_service or "unknown", voice_model, game_voice_type, game_time),
                )
                return voice_model
        if row[0] != voice_model:
            raise VoiceBindingConflict(
                f"npc_id {npc_id} is bound to voice '{row[0]}' but Mantella chose '{voice_model}'")
        return row[0]

    def voice_of(self, npc_id: int) -> str | None:
        return self.store.scalar("SELECT voice_model FROM voice_binding WHERE npc_id = ?", (npc_id,))
