"""Save reloads: loading an older save rolls every NPC's memory back to that save.

Each save carries the id of the timeline it was on (Papyrus keeps it in a script variable, which
the game stores in the save). When a save loads, the game reports that id and the save's game
time. If that timeline already holds rows from later than the save, those events have not
happened in the loaded game, so a new timeline starts, branching from the old one at the save's
game time. Nothing is deleted: the views ``v_timeline_scope``, ``v_known_facts`` and
``v_visible_memory`` simply stop showing rows from after the branch point, and loading the later
save again makes its timeline active again.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.store.world_store import WorldStore

EPSILON_DAYS = 0.0007  # about one game minute


@dataclass(frozen=True)
class LoadResult:
    timeline_id: int
    branched: bool
    repeated: bool  # this load was already handled


def latest_game_time(db, timeline_id: int) -> float | None:
    return db.execute(
        "SELECT MAX(t) FROM ("
        " SELECT MAX(COALESCE(ended_gt, started_gt)) t FROM conversation WHERE timeline_id = :t"
        " UNION ALL SELECT MAX(game_time) FROM utterance WHERE timeline_id = :t"
        " UNION ALL SELECT MAX(learned_gt) FROM knowledge WHERE timeline_id = :t"
        " UNION ALL SELECT MAX(occurred_gt) FROM memory WHERE timeline_id = :t"
        " UNION ALL SELECT MAX(game_time) FROM hygiene_change WHERE timeline_id = :t AND cause IN ('fight','swim','bath'))",
        {"t": timeline_id}).fetchone()[0]


def resolve_load(store: WorldStore, nonce: int, save_timeline_id: int | None, loaded_gt: float,
                 save_name: str | None = None) -> LoadResult:
    with store.transaction() as db:
        seen = db.execute("SELECT timeline_id FROM game_load WHERE nonce = ?", (nonce,)).fetchone()
        if seen:
            return LoadResult(seen[0], False, True)

        active = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        known = save_timeline_id is not None and db.execute(
            "SELECT 1 FROM timeline WHERE timeline_id = ?", (save_timeline_id,)).fetchone()
        base = save_timeline_id if known else active   # a save from before the Living World: current timeline

        latest = latest_game_time(db, base)
        branched = latest is not None and latest > loaded_gt + EPSILON_DAYS
        db.execute("UPDATE timeline SET is_active = 0 WHERE is_active = 1")
        if branched:
            target = db.execute(
                "INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (?,?,?,1)",
                (base, loaded_gt, save_name)).lastrowid
        else:
            target = base
            db.execute("UPDATE timeline SET is_active = 1 WHERE timeline_id = ?", (target,))
        db.execute("INSERT INTO game_load (nonce, save_timeline_id, loaded_gt, timeline_id, branched) VALUES (?,?,?,?,?)",
                   (nonce, save_timeline_id, loaded_gt, target, int(branched)))
        return LoadResult(target, branched, False)
