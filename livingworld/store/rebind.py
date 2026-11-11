"""A new placed reference of an NPC already known keeps their identity.

The LWSaloniaSurrender plugin swaps Salonia Carvain's road-encounter reference (the game deletes it after a
while) for a persistent copy of the same base. The copy has another form ID, so Mantella and the Living
World took her for a stranger: no memories, no surrender, an empty summary folder. The game (Mantella-Spell
``OnLwSurrenderReplace``) queues a record::

    v=1|key=812345-13|type=rebind|gt=4.5|actor=<new reference>|target=<old reference>

Before the conversation's people are registered, the new reference becomes an alias of the old one's
``npc_id`` (``npc_ref_alias``, migration 016), and Mantella's own folder for the old reference
(``<world>/<name> - <old 6-digit id>``: summaries and transcript) is copied to the new one's name, unless
that folder already exists. Nothing is moved or deleted, so an older save with the old reference still works.
"""
from __future__ import annotations

import logging
import os
import shutil

from livingworld.perception.records import BadRecord, PerceptionRecord, parse_record
from livingworld.store.identity import ActorIdentity, IdentityRegistry
from livingworld.store.world_store import WorldStore

logger = logging.getLogger("livingworld.rebind")


def is_rebind_record(raw: str) -> bool:
    low = (raw or "").lower()
    return "|type=rebind|" in low or low.endswith("|type=rebind")


def alias_of(db, identity: ActorIdentity) -> int | None:
    row = db.execute("SELECT npc_id FROM npc_ref_alias WHERE plugin = ? AND base_form_id = ? AND ref_form_id = ?",
                     (identity.base.plugin, identity.base.local_id, identity.ref.full)).fetchone()
    return row[0] if row else None


def apply(store: WorldStore, record: PerceptionRecord) -> int | None:
    """Make the new reference (``actor``) resolve to the old one's npc_id (``target``). Returns that npc_id."""
    new, old = record.actor.identity(), record.target.identity()
    if new.ref.full == old.ref.full:
        return None
    npc_id = IdentityRegistry(store).ensure_npc(old, record.game_time)
    with store.transaction() as db:
        existing = db.execute("SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? AND ref_form_id = ?",
                              (new.base.plugin, new.base.local_id, new.ref.full)).fetchone()
        if existing and existing[0] != npc_id:
            # the copy was already met as a stranger: point its own row's reference elsewhere so it never matches again
            db.execute("UPDATE npc SET ref_form_id = ref_form_id || '|replaced' WHERE npc_id = ?", (existing[0],))
        db.execute("INSERT OR REPLACE INTO npc_ref_alias (plugin, base_form_id, ref_form_id, npc_id, created_gt)"
                   " VALUES (?,?,?,?,?)", (new.base.plugin, new.base.local_id, new.ref.full, npc_id, record.game_time))
    logger.log(23, f"Living World: {new.name} {new.ref.mantella_id} is {old.name} {old.ref.mantella_id} (npc_id {npc_id})")
    return npc_id


def copy_mantella_folder(conversations_root: str, world_id: str, record: PerceptionRecord) -> str | None:
    """Copy Mantella's folder for the old reference to the new reference's name, when it has none yet."""
    if not conversations_root or not world_id:
        return None
    new, old = record.actor.identity(), record.target.identity()
    base = os.path.join(conversations_root, world_id)
    src = os.path.join(base, f"{old.name} - {old.ref.mantella_id}")
    dst = os.path.join(base, f"{new.name} - {new.ref.mantella_id}")
    if not os.path.isdir(src) or os.path.exists(dst):
        return None
    shutil.copytree(src, dst)   # files inside are named after the NPC, not the reference
    logger.log(23, f"Living World: copied Mantella's folder {src} -> {dst}")
    return dst


def parse(raw: str) -> PerceptionRecord | None:
    try:
        record = parse_record(raw)
    except (BadRecord, ValueError) as e:
        logger.error(f"Living World: rebind record skipped ({e}): {raw[:200]}")
        return None
    return record if record.type == "rebind" else None
