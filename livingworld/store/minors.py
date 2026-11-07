"""Child NPCs are left out of everything Living World works out: witnesses, memories of grabs,
clothing, drinking, gossip, reputation and goals. Mantella's own conversations with them still work.

A child is recognised by any of:

* a Mantella character entry whose voice is a child voice (``data/Skyrim/skyrim_characters.csv``:
  ``MaleChild`` / ``FemaleChild``), matched on the plugin-relative base form ID;
* a race or voice type the game reports with "Child" in it (vanilla child races and voice types);
* the game itself (the Living World Papyrus never sends a record about a child and never lists one
  as a witness; ``Actor.IsChild()``).

The first two are checked here, so the server holds the line even with an older game-side build.
"""
from __future__ import annotations

import csv
import functools
import os

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "Skyrim", "skyrim_characters.csv")


@functools.lru_cache(maxsize=1)
def child_base_ids() -> frozenset[str]:
    """Plugin-relative base IDs ('0x0001347E') of every child in Mantella's character list."""
    found: set[str] = set()
    try:
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                voices = " ".join(row.get(k) or "" for k in ("voice_model", "skyrim_voice_folder", "advanced_voice_model"))
                base = (row.get("base_id") or "").strip()
                if "child" in voices.lower() and base:
                    found.add("0x" + base.upper().rjust(8, "0"))
    except OSError:
        pass
    return frozenset(found)


def looks_like_child(base_form_id: str | None = None, race: str | None = None, voice: str | None = None) -> bool:
    if race and "child" in race.lower():
        return True
    if voice and "child" in voice.lower():
        return True
    if base_form_id and base_form_id.upper().replace("0X", "0x") in child_base_ids():
        return True
    return False


def mark_children(db) -> int:
    """Flag every known NPC who is a child. Returns how many were newly flagged."""
    rows = db.execute("SELECT npc_id, base_form_id, race FROM npc WHERE is_child = 0 AND is_player = 0").fetchall()
    ids = [r[0] for r in rows if looks_like_child(r[1], r[2])]
    db.executemany("UPDATE npc SET is_child = 1 WHERE npc_id = ?", [(i,) for i in ids])
    return len(ids)


def is_child(db, npc_id: int | None) -> bool:
    if npc_id is None:
        return False
    row = db.execute("SELECT is_child, base_form_id, race FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row:
        return False
    if row[0]:
        return True
    if looks_like_child(row[1], row[2]):
        db.execute("UPDATE npc SET is_child = 1 WHERE npc_id = ?", (npc_id,))
        return True
    return False
