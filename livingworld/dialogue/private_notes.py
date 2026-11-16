"""Notes for one NPC only, carried on the player's line.

Living World adds notes for the dialogue model to the player's line (Mantella's in-game events). Some of
them are what one NPC alone knows, feels or sees: what Hod remembers of a place, what Hod noticed the
player doing, the field report Caius gets about the job he gave, a woman's own preferences. In a group
conversation every NPC's prompt is built from the same thread, and at the end every NPC's summary is
written from it too, so without a filter Lucan would read (and later remember) Hod's memory.

A ``PrivateNote`` is a plain string that also names its owner (the game ``ref_id`` of that NPC, which
stays the same for the whole conversation). ``view_for`` gives the player's line as one NPC sees it:
their own private notes and every shared note, but no other NPC's private notes. Things said aloud,
names learned aloud, the player's yes / no answers, touch the NPC agreed to out loud and arrests stay
shared notes: everyone present heard or saw them.

A note whose owner is unknown, or a viewer whose ref_id is unknown, is never shown to anyone but an
exact owner match: when in doubt, leave it out.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

_EVENTS = "_UserMessage__ingame_events"


class PrivateNote(str):
    """A note string that only its owner's prompt (and summary) may hold."""

    owner: str | None

    def __new__(cls, text: str = "", owner: Any = None) -> "PrivateNote":
        note = super().__new__(cls, text)
        note.owner = None if owner is None else str(owner)
        return note

    def __reduce__(self):   # deepcopy / pickle keep the owner (get_talk_only deep-copies messages)
        return (PrivateNote, (str(self), self.owner))


def private(notes: Iterable[str], owner: Any) -> list[str]:
    """Each note marked as ``owner``'s alone."""
    return [PrivateNote(n, owner) for n in notes if n]


def reowned(notes: Iterable[str], owners: dict[Any, Any]) -> list[str]:
    """Private notes whose owner is a Living World npc_id, re-keyed to that NPC's game ref_id through
    ``owners`` (npc_id -> ref_id). A private note with no ref_id keeps an owner nobody matches."""
    out: list[str] = []
    for n in notes:
        if isinstance(n, PrivateNote):
            ref = owners.get(n.owner)
            if ref is None:
                try:
                    ref = owners.get(int(n.owner)) if n.owner is not None else None
                except ValueError:
                    ref = None
            out.append(PrivateNote(str(n), ref if ref is not None else f"npc:{n.owner}"))
        else:
            out.append(n)
    return out


def visible_to(note: str, viewer_ref_id: Any) -> bool:
    if not isinstance(note, PrivateNote):
        return True
    return viewer_ref_id is not None and note.owner is not None and note.owner == str(viewer_ref_id)


def has_private(message: Any) -> bool:
    return any(isinstance(e, PrivateNote) for e in getattr(message, _EVENTS, None) or [])


def view_for(message: Any, viewer_ref_id: Any) -> Any:
    """``message`` as the NPC with ``viewer_ref_id`` sees it: the same object when nothing is hidden
    from them, otherwise a shallow copy without the other NPCs' private notes. The thread is not changed."""
    events = getattr(message, _EVENTS, None)
    if not events:
        return message
    kept = [e for e in events if visible_to(e, viewer_ref_id)]
    if len(kept) == len(events):
        return message
    view = copy.copy(message)
    setattr(view, _EVENTS, kept)
    return view
