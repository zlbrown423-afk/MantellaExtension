"""How big the player is, as The New Gentleman (TNG) shows it, and what women in a scene make of it.

TNG sizes a man by scaling one bone, "NPC GenitalsBase [GenBase]", by his size class (XS 0.8, S 0.9, M 1.0,
L 1.2, XL 1.4 by default, set in its MCM) times his race's multiplier (TNG 4.2.7, ``Core::SetActorSize``).
The game reads that bone's scale and sends it as ``psize`` on scene records. The scale is uniform, so length
and girth grow together. What a scale of 1.0 measures depends on the BodySlide Length and Thickness sliders
the mesh was built with, which the game cannot read, so it is set here:

* ``MANTELLA_LW_TNG_LENGTH_IN``: length in inches at scale 1.0 (default 6.0)
* ``MANTELLA_LW_TNG_GIRTH_IN``: girth (around) in inches at scale 1.0 (default 4.75)

Outside scenes, women who have been with him remember it and may bring it up in later conversations
with him, and women who see him naked notice it (see below).

The rule the user asked for: 5 inches or less is unimpressive, more than that is impressive. Girth adds its own
word: under 4.25 inches around is thin, 5.25 or more is thick. Only women react, and never to a forced scene.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

LENGTH_ENV = "MANTELLA_LW_TNG_LENGTH_IN"
GIRTH_ENV = "MANTELLA_LW_TNG_GIRTH_IN"
DEFAULT_LENGTH_IN = 6.0
DEFAULT_GIRTH_IN = 4.75

IMPRESSIVE_OVER_IN = 5.0      # more than this is impressive; this or less is not
THIN_UNDER_IN = 4.25
THICK_FROM_IN = 5.25


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if 1.0 <= value <= 20.0 else default


@dataclass(frozen=True)
class Size:
    length_in: float
    girth_in: float

    @property
    def impressive(self) -> bool:
        return self.length_in > IMPRESSIVE_OVER_IN

    @property
    def girth_word(self) -> str | None:
        if self.girth_in < THIN_UNDER_IN:
            return "thin"
        if self.girth_in >= THICK_FROM_IN:
            return "thick"
        return None

    def measure(self) -> str:
        return f"about {self.length_in:.1f} inches long and {self.girth_in:.1f} inches around"

    def verdict(self) -> str:
        """'impressive, and thick' / 'unimpressive, though thick' / 'impressive'."""
        word = "impressive" if self.impressive else "unimpressive"
        girth = self.girth_word
        if girth is None:
            return word
        if girth == "thick":
            return f"{word}, and thick" if self.impressive else f"{word} in length, though thick"
        return f"{word} in length, but thin" if self.impressive else f"{word}, and thin"


def from_scale(scale: float | None) -> Size | None:
    """The player's size from the bone scale the game sent; None when it sent none (no TNG, not a man)."""
    if scale is None or scale <= 0.05 or scale > 5.0:
        return None
    return Size(round(_env_float(LENGTH_ENV, DEFAULT_LENGTH_IN) * scale, 1),
                round(_env_float(GIRTH_ENV, DEFAULT_GIRTH_IN) * scale, 1))


def prompt_line(name: str, player: str, size: Size) -> str:
    return (f"{name} has seen {player}'s manhood up close: {size.measure()}. {name} finds it {size.verdict()}"
            f" (to {name}, five inches or less is unimpressive, anything bigger impressive), and may show it.")


def memory_line(player: str, size: Size) -> str:
    return f" {player} was {size.measure()}; I found him {size.verdict()}."


# ---- outside scenes --------------------------------------------------------------------------------
# Women who have been with the player remember his size and may bring it up in any later conversation with
# him (tease, compliment, compare); women who see him naked outside a scene notice it and remember it.

def size_enabled() -> bool:
    from livingworld.intimacy import intimacy_enabled
    return intimacy_enabled()


def seen_line(name: str, player: str, size: Size) -> str:
    return (f"{name} can see {player}'s manhood: {size.measure()}. {name} finds it {size.verdict()}"
            f" and may remark on it, teasing or admiring.")


def seen_memory(player: str, size: Size) -> str:
    return f" He was {size.measure()}; I found him {size.verdict()}."


def remembered_line(db, npc_id: int, name: str, other_id: int, other_name: str) -> str | None:
    """For a woman who has been intimate with ``other`` (the player, in an ordinary scene): what she remembers of
    his size, for any conversation with him. None when she has not, or no size was sent."""
    from livingworld.perception.attire import sex_of
    if sex_of(db, npc_id) != "female":
        return None
    row = db.execute(
        "SELECT json_extract(payload_json, '$.player_size') FROM v_visible_event WHERE type = 'romance.scene'"
        " AND json_extract(actors_json, '$[0]') = ? AND EXISTS (SELECT 1 FROM json_each(actors_json) j WHERE j.value = ?)"
        " AND json_extract(payload_json, '$.player_size') IS NOT NULL ORDER BY game_time DESC, event_id DESC LIMIT 1",
        (other_id, npc_id)).fetchone()
    size = from_scale(row[0]) if row else None
    if size is None:
        return None
    return (f"{name} has been intimate with {other_name} and remembers his size: {size.measure()}, which she found"
            f" {size.verdict()}. She may bring it up when it fits (teasing, praise, or comparing), more readily"
            f" in private or after a drink.")
