"""Mantella's own conversation summaries roll back with the save.

Mantella keeps a text file per NPC (``<world>/<name> - <ref>/<name>_summary_N.txt``) and adds a paragraph after
every conversation. Living World memories hide everything after an older save's point (``v_timeline_scope``),
but these files did not: after a reload an NPC still remembered a conversation that, for the game, never
happened (Salonia Carvain, 2026-11-11).

Each line Mantella appends is noted (``summary_line``: folder, a hash of the line, timeline, game time), and so
is each new file a resummary starts (``summary_file``). Reading:

* a line noted only on timelines or at times the current save cannot see is left out; a line never noted
  (written before this existed, or by hand) is kept;
* a file started by a resummary the current save cannot see is passed over for the newest file it can see
  (the resummary folded later conversations into it, so it cannot be split).

Nothing in the files is changed or deleted, so loading the later save again brings it all back.
"""
from __future__ import annotations

import hashlib


def _hash(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()


def note_lines(db, timeline: int, folder: str, text: str, gt: float) -> int:
    n = 0
    for line in text.splitlines():
        if line.strip():
            db.execute("INSERT INTO summary_line (timeline_id, folder, line_hash, game_time) VALUES (?,?,?,?)",
                       (timeline, folder, _hash(line), gt))
            n += 1
    return n


def note_file(db, timeline: int, folder: str, number: int, gt: float) -> None:
    db.execute("INSERT INTO summary_file (timeline_id, folder, file_number, game_time) VALUES (?,?,?,?)",
               (timeline, folder, number, gt))


def visible_lines(db, folder: str, lines: list[str]) -> list[str]:
    """``lines`` without the ones written only where the current save cannot see."""
    noted = {}
    for line_hash, seen in db.execute(
            "SELECT l.line_hash, MAX(CASE WHEN s.timeline_id IS NOT NULL THEN 1 ELSE 0 END) FROM summary_line l"
            " LEFT JOIN v_timeline_scope s ON s.timeline_id = l.timeline_id AND l.game_time <= s.cutoff_gt"
            " WHERE l.folder = ? GROUP BY l.line_hash", (folder,)).fetchall():
        noted[line_hash] = bool(seen)
    return [line for line in lines if noted.get(_hash(line), True)]


def visible_file(db, folder: str, numbers: list[int]) -> int | None:
    """The newest of ``numbers`` the current save can see: a file no resummary noted (the first one, or
    older than this) counts as seen."""
    hidden = set()
    for number, seen in db.execute(
            "SELECT f.file_number, MAX(CASE WHEN s.timeline_id IS NOT NULL THEN 1 ELSE 0 END) FROM summary_file f"
            " LEFT JOIN v_timeline_scope s ON s.timeline_id = f.timeline_id AND f.game_time <= s.cutoff_gt"
            " WHERE f.folder = ? GROUP BY f.file_number", (folder,)).fetchall():
        if not seen:
            hidden.add(number)
    for n in sorted(numbers, reverse=True):
        if n not in hidden:
            return n
    return None
