"""Where a place name stands against an assignment."""
from __future__ import annotations

WILDERNESS = frozenset({"skyrim", "tamriel"})   # the game's name for open country with no place of its own

ASSIGNED, OPEN_ROAD, WILDS, ELSEWHERE = "assigned", "open_road", "wilds", "elsewhere"


def relation(where: tuple[str, ...] | list[str], location: str | None, open_country: bool = False) -> str:
    loc = (location or "").strip().lower()
    if loc and any(w.strip().lower() in loc for w in where if w.strip()):
        return ASSIGNED
    if not loc or loc in WILDERNESS:
        return OPEN_ROAD if open_country else WILDS
    return ELSEWHERE
