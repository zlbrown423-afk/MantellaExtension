"""The raw conversation log: who talked, in what order, with which voice. Append-only.

Rows are written as each line is spoken, not at the end, so a game crash loses at most the
line being generated. Later milestones extract memories from these rows; nothing edits them.
"""
from __future__ import annotations

from livingworld.store.world_store import WorldStore


class ConversationLog:
    def __init__(self, store: WorldStore) -> None:
        self.store = store

    def open(self, location: str, kind: str, game_time: float) -> int:
        with self.store.transaction() as db:
            timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
            cur = db.execute(
                "INSERT INTO conversation (timeline_id, location, kind, started_gt) VALUES (?,?,?,?)",
                (timeline, location or "unknown", kind, game_time),
            )
            return cur.lastrowid

    def add_participant(self, conv_id: int, npc_id: int) -> bool:
        """Add a participant; returns True if they were new to this conversation."""
        with self.store.transaction() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM utterance WHERE conv_id = ?", (conv_id,)).fetchone()[0]
            cur = db.execute(
                "INSERT OR IGNORE INTO participant (conv_id, npc_id, joined_seq) VALUES (?,?,?)",
                (conv_id, npc_id, seq),
            )
            if cur.rowcount == 0:
                # rejoining after leaving clears left_seq
                db.execute("UPDATE participant SET left_seq = NULL WHERE conv_id = ? AND npc_id = ? AND left_seq IS NOT NULL",
                           (conv_id, npc_id))
            return cur.rowcount == 1

    def mark_left(self, conv_id: int, npc_id: int) -> None:
        with self.store.transaction() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM utterance WHERE conv_id = ?", (conv_id,)).fetchone()[0]
            db.execute("UPDATE participant SET left_seq = ? WHERE conv_id = ? AND npc_id = ? AND left_seq IS NULL",
                       (seq, conv_id, npc_id))

    def append(self, conv_id: int, speaker_id: int, text: str, voice_model: str | None, game_time: float) -> int:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT c.timeline_id, COALESCE(MAX(u.seq), 0) + 1 FROM conversation c"
                " LEFT JOIN utterance u ON u.conv_id = c.conv_id WHERE c.conv_id = ?",
                (conv_id,),
            ).fetchone()
            cur = db.execute(
                "INSERT INTO utterance (timeline_id, conv_id, seq, speaker_id, text, voice_model, game_time)"
                " VALUES (?,?,?,?,?,?,?)",
                (row[0], conv_id, row[1], speaker_id, text, voice_model, game_time),
            )
            return cur.lastrowid

    def close(self, conv_id: int, game_time: float) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE conversation SET ended_gt = ? WHERE conv_id = ? AND ended_gt IS NULL",
                       (game_time, conv_id))
