"""Places an NPC mentions get their map marker added for the player (undiscovered), once.

``data/map_markers.json`` lists the vanilla map markers by name (made from the game's own files with
``tools/extract_map_markers.py``). A name that more than one marker shares ("Abandoned Shack") is left out,
since nobody can tell which one was meant. Matching is on NPC lines only and case-sensitive: the language
model spells and capitalises place names right.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from importlib import resources

from livingworld.fog import fog_enabled

DATA_PACKAGE = "livingworld.fog.data"
DATA_FILE = "map_markers.json"


@dataclass(frozen=True)
class Marker:
    name: str
    plugin: str
    form_id: str    # '0x0001A2B3', plugin-relative

    @property
    def form_int(self) -> int:
        return int(self.form_id, 16)


@functools.lru_cache(maxsize=1)
def markers() -> tuple[Marker, ...]:
    try:
        raw = json.loads(resources.files(DATA_PACKAGE).joinpath(DATA_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return ()
    counts: dict[str, int] = {}
    for m in raw:
        counts[m["name"]] = counts.get(m["name"], 0) + 1
    return tuple(Marker(m["name"], m["plugin"], m["form_id"]) for m in raw
                 if counts[m["name"]] == 1 and len(m["name"]) >= 4)


@functools.lru_cache(maxsize=1)
def _matcher() -> tuple[re.Pattern | None, dict[str, Marker]]:
    by_name = {m.name: m for m in markers()}
    if not by_name:
        return None, {}
    alts = sorted((re.escape(n) for n in by_name), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z'\-])(" + "|".join(alts) + r")(?![A-Za-z\-])"), by_name


def mentioned(text: str) -> list[Marker]:
    """The map markers named in ``text``, in the order they come."""
    pattern, by_name = _matcher()
    if pattern is None or not text:
        return []
    out: list[Marker] = []
    for m in pattern.finditer(text):
        marker = by_name[m.group(1)]
        if marker not in out:
            out.append(marker)
    return out


def heard_of(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM place_known p JOIN v_timeline_scope s ON s.timeline_id = p.timeline_id"
        " AND p.game_time <= s.cutoff_gt WHERE p.name = ? LIMIT 1", (name,)).fetchone() is not None


def npc_line(db, text: str, speaker_id: int, game_time: float, conv_id: int | None) -> list[Marker]:
    """Places named in an NPC's line that the player had not heard of: recorded, and returned for the game."""
    if not fog_enabled():
        return []
    new = []
    for marker in mentioned(text):
        if heard_of(db, marker.name):
            continue
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        db.execute("INSERT INTO place_known (timeline_id, name, plugin, marker_form_id, how, told_by_id, conv_id,"
                   " game_time) VALUES (?,?,?,?, 'told', ?,?,?)",
                   (timeline, marker.name, marker.plugin, marker.form_id, speaker_id, conv_id, game_time))
        new.append(marker)
    return new
