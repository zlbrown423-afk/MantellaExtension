"""Confirmed corrections to misheard speech (migration 029).

The speech recognition sometimes hears the wrong word (2026-11-22: the player said "you've shown me everything
tonight" to Gisli and it was stored as "served"). The raw transcript stays as it was; a correction row gives the
line its canonical text, which everything that reads ``v_utterance`` uses from then on: memory extraction, the
recap of an earlier conversation, picking a conversation back up, and so diaries built from those memories.

    python -m livingworld.memory.corrections <world.sqlite> <utt_id> "<corrected text>" [--note "..."] [--apply]

Without ``--apply`` it only shows what would change. Correcting a line again supersedes the earlier correction
(both stay). Only the words may change: the line keeps its id, timeline, conversation, time and speaker.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass

SOURCES = ("player_confirmed", "admin")


@dataclass
class Correction:
    utt_id: int
    raw_text: str
    before: str          # the canonical text before this correction (raw, or an earlier correction)
    after: str
    corr_id: int | None = None


def correct(db: sqlite3.Connection, utt_id: int, text: str, source: str = "player_confirmed",
            note: str | None = None, apply: bool = True) -> Correction:
    """Record ``text`` as the canonical words of line ``utt_id``. Refuses an unknown line, empty text or a
    correction that changes nothing."""
    text = (text or "").strip()
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    row = db.execute("SELECT raw_text, text FROM v_utterance WHERE utt_id = ?", (utt_id,)).fetchone()
    if row is None:
        raise ValueError(f"no line {utt_id}")
    if not text:
        raise ValueError("empty correction")
    raw, current = row[0], row[1]
    result = Correction(utt_id, raw, current, text)
    if text == current:
        raise ValueError("the line already reads that way")
    if not apply:
        return result
    cur = db.execute("INSERT INTO utterance_correction (utt_id, raw_text, corrected_text, source, note)"
                     " VALUES (?,?,?,?,?)", (utt_id, raw, text, source, note))
    result.corr_id = cur.lastrowid
    db.execute("UPDATE utterance_correction SET superseded_by = ? WHERE utt_id = ? AND corr_id <> ?"
               " AND superseded_by IS NULL", (result.corr_id, utt_id, result.corr_id))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("world")
    ap.add_argument("utt_id", type=int)
    ap.add_argument("text")
    ap.add_argument("--note")
    ap.add_argument("--source", default="player_confirmed", choices=SOURCES)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    from livingworld.store.world_store import WorldStore
    store = WorldStore(a.world)   # opening it runs any pending migration (with its own backup)
    with store.transaction() as db:
        c = correct(db, a.utt_id, a.text, a.source, a.note, a.apply)
    print(f"line {c.utt_id}\n  heard:  {c.raw_text}\n  before: {c.before}\n  after:  {c.after}")
    print("applied" if a.apply else "not applied (add --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
