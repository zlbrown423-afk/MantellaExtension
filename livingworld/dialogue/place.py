"""Where the NPC is right now: outdoors, or inside a named place.

The game names only the player's current location, and out in a city that is just "Whiterun". Each NPC's
bio says where they work and live ("lives in their shop, Warmaiden's"), so with nothing else to go on
the AI talks as if the NPC were standing in their shop: "What brings you to Warmaiden's?" in the
street, "come on inside" when they are already outside, a guard on the road offering their wares.

The game sends the weather only when the player is outdoors, so the start of a conversation tells us
which: outdoors gets "out in the open, not inside any building"; indoors names the place, and inside a
building the game does not name (location still "Whiterun") the NPC is told not to assume it is their own.
A later "(The location is now ...)" note in the conversation still wins.

On with the Living World unless ``MANTELLA_LW_PLACE=0``. No extra AI call.
"""
from __future__ import annotations

import os

PLACE_ENV = "MANTELLA_LW_PLACE"
_OFF = {"0", "false", "no", "off"}

# cities, towns and villages: a location by one of these names is the settlement itself, not one building
SETTLEMENTS = {
    "skyrim", "whiterun", "solitude", "windhelm", "markarth", "riften", "falkreath", "dawnstar", "morthal",
    "winterhold", "riverwood", "rorikstead", "ivarstead", "dragon bridge", "karthwasten", "shor's stone",
    "kynesgrove", "helgen", "darkwater crossing", "old hroldan", "soljund's sinkhole", "stonehills",
    "raven rock", "skaal village", "thirsk mead hall", "heartwood mill", "half-moon mill", "anga's mill",
    "mixwater mill", "the reach", "the pale", "the rift", "eastmarch", "haafingar", "hjaalmarch",
    "falkreath hold", "whiterun hold", "winterhold hold", "solstheim",
}

_OUTDOORS = ("Outdoors in {place}, out in the open (street, square, yard or road), not inside any building.",
             "Shops, inns, halls and homes are indoors, so do not talk as if you were inside your own shop, "
             "stall or home, and do not say 'here at' a building. If the player wants to see your goods or your "
             "home, say you can head there, or where to find it.")
_INSIDE_UNNAMED = ("Indoors, inside a building in {place}; the game does not say which.",
                   "Do not assume it is your own shop or home unless you know it is.")
_INSIDE = ("Indoors, in {place}.",)
_LATER = "If a note later says the location changed, go by that."


def place_enabled() -> bool:
    return os.environ.get(PLACE_ENV, "").strip().lower() not in _OFF


def prompt_lines(location: str, outdoors: bool) -> list[str]:
    """The lines for the prompt. ``outdoors``: the game sent weather with the start of the conversation."""
    place = (location or "").strip() or "Skyrim"
    if outdoors:
        lines = _OUTDOORS
    elif place.lower() in SETTLEMENTS:
        lines = _INSIDE_UNNAMED
    else:
        lines = _INSIDE
    return [line.format(place=place) for line in lines] + [_LATER]
