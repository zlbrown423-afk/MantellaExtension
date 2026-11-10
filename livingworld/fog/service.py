"""Who knows whose name, on the save timeline (``name_known`` / ``v_name_known``).

Only the player and the NPCs they talk to are fogged: NPCs are taken to know each other, as neighbours do.
All functions take an open connection; the caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from livingworld.fog import fog_enabled, names


@dataclass(frozen=True)
class Person:
    npc_id: int
    name: str                   # the real name (Mantella's)
    label: str | None = None    # what the game shows while unknown; None = never hidden


@dataclass
class Learned:
    """What one line taught: names the player learned (for the game) and log lines."""
    player_learned: list[str] = field(default_factory=list)   # real names of people the player now knows
    log: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # for the dialogue model, with the player's next line


def _timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def known_name(db, knower_id: int, subject_id: int) -> str | None:
    """The name ``knower`` knows ``subject`` by, or None if they have not learned it."""
    row = db.execute("SELECT name FROM v_name_known WHERE knower_id = ? AND subject_id = ?",
                     (knower_id, subject_id)).fetchone()
    return row[0] if row else None


def learn(db, knower_id: int, subject_id: int, name: str, how: str, game_time: float,
          told_by_id: int | None = None, conv_id: int | None = None) -> bool:
    """Record that ``knower`` now knows ``subject`` as ``name``. False when they already knew that name."""
    if knower_id == subject_id or not name:
        return False
    if known_name(db, knower_id, subject_id) == name:
        return False
    db.execute("INSERT INTO name_known (timeline_id, knower_id, subject_id, name, how, told_by_id, conv_id, game_time)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (_timeline(db), knower_id, subject_id, name, how, told_by_id, conv_id, game_time))
    return True


def set_label(db, npc_id: int, label: str | None) -> None:
    db.execute("UPDATE npc SET stranger_label = ? WHERE npc_id = ? AND stranger_label IS NOT ?",
               (label or None, npc_id, label or None))


def player_id(db) -> int | None:
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
    return row[0] if row else None


def known_to_player(db, player: int) -> list[str]:
    """Real names of everyone the player knows (the game shows these people by name)."""
    rows = db.execute("SELECT n.display_name FROM v_name_known k JOIN npc n ON n.npc_id = k.subject_id"
                      " WHERE k.knower_id = ? ORDER BY n.display_name", (player,)).fetchall()
    return sorted({r[0] for r in rows})


def _player_label(db, player: int) -> str:
    row = db.execute("SELECT stranger_label, race, sex FROM npc WHERE npc_id = ?", (player,)).fetchone()
    if row and row[0]:
        return row[0]
    if row and (row[1] or row[2]):
        noun = {"male": "man", "female": "woman"}.get(row[2] or "", "person")
        return f"{row[1]} {noun}" if row[1] else noun
    return "traveller"


def _article(label: str) -> str:
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def shown_name(db, viewer: int, subject: int, real: str | None = None) -> str:
    """How ``viewer`` would name ``subject``: the real name if known (or never hidden), else a description.
    Used for hints the player reads and for gossip an NPC holds about the player."""
    if real is None:
        row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (subject,)).fetchone()
        real = row[0] if row else "someone"
    if not fog_enabled() or viewer == subject:
        return real
    row = db.execute("SELECT is_player, stranger_label FROM npc WHERE npc_id = ?", (subject,)).fetchone()
    viewer_is_player = bool(db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (viewer,)).fetchone()[0])
    if row and row[0]:   # an NPC thinking of the player
        if not row[1]:   # the game build does not take part in the fog (it sent no description of the player)
            return real
        return known_name(db, viewer, subject) or f"a stranger ({_article(row[1])})"
    if viewer_is_player and row and row[1]:
        return real if known_name(db, viewer, subject) else _article(row[1])
    return real


def prompt_lines(db, npc: Person, player: Person) -> list[str]:
    """What this NPC's prompt says about names between them and the player (empty when nothing is hidden)."""
    if not fog_enabled():
        return []
    lines = []
    told = known_name(db, npc.npc_id, player.npc_id)
    if told is None:
        lines.append(
            f"You have never been told the name of the {_player_label(db, player.npc_id)} you are talking to. "
            f"This transcript labels them '{player.name}' only so you can follow who is speaking; you do not know "
            f"that name, so never say it. Call them stranger, traveller, friend or the like, until they tell you "
            f"their name or someone here says it.")
    elif told != player.name and not names.says_name(told, player.name, ignore_case=True):
        lines.append(f"{player.name} told you their name is {told}. You know them as {told} and call them that.")
    if npc.label and known_name(db, player.npc_id, npc.npc_id) is None:
        lines.append(
            f"{player.name} does not know your name yet: to them you are just a {npc.label}. Say your name when "
            f"you would naturally introduce yourself (meeting someone new, or when asked), not otherwise.")
    return lines


def hide_player_name(db, npc_id: int, player: Person, text: str) -> str:
    """Recalled memories mention the player by name; an NPC who was never told it reads 'the stranger'."""
    if not text or not fog_enabled() or known_name(db, npc_id, player.npc_id) is not None:
        return text
    import re
    for name in names.call_names(player.name):
        text = re.sub(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", "the stranger", text)
    return text


def npc_line(db, speaker: Person, text: str, present: Iterable[Person], player: Person, game_time: float,
             conv_id: int | None) -> Learned:
    """Names said in an NPC's line: their own (the player learns it), another person's who is in the
    conversation (the player learns it), and the player's, as the speaker knows it (the others learn it)."""
    out = Learned()
    if not text:
        return out
    others = [p for p in present if p.npc_id not in (speaker.npc_id, player.npc_id)]
    if names.says_name(text, speaker.name):
        if learn(db, player.npc_id, speaker.npc_id, speaker.name, "introduced", game_time, conv_id=conv_id):
            out.player_learned.append(speaker.name)
            out.log.append(f"{speaker.name} said their name to the player")
            out.notes.append(f"{player.name} now knows {speaker.name}'s name.")
    for other in others:
        if names.says_name(text, other.name):
            if learn(db, player.npc_id, other.npc_id, other.name, "named", game_time, speaker.npc_id, conv_id):
                out.player_learned.append(other.name)
                out.log.append(f"{speaker.name} named {other.name} in front of the player")
                out.notes.append(f"{player.name} now knows {other.name}'s name.")
    told = known_name(db, speaker.npc_id, player.npc_id)
    if told and names.says_name(text, told):
        for other in others:
            if learn(db, other.npc_id, player.npc_id, told, "named", game_time, speaker.npc_id, conv_id):
                out.log.append(f"{other.name} heard {speaker.name} call the player {told}")
                out.notes.append(_knows_player(other, player, told))
    return out


def player_line(db, player: Person, text: str, present: Iterable[Person], game_time: float,
                conv_id: int | None) -> Learned:
    """The player introducing themselves (everyone present learns the name they gave), and the player
    saying the name of someone in the conversation (the player clearly knows it)."""
    out = Learned()
    if not text:
        return out
    present = [p for p in present if p.npc_id != player.npc_id]
    given = names.player_introduction(text, player.name)
    if given:
        for npc in present:
            if learn(db, npc.npc_id, player.npc_id, given, "introduced", game_time, conv_id=conv_id):
                out.log.append(f"the player told {npc.name} their name is {given}")
                out.notes.append(_knows_player(npc, player, given))
    for npc in present:
        if npc.label and names.says_name(text, npc.name, ignore_case=True):
            if learn(db, player.npc_id, npc.npc_id, npc.name, "said", game_time, conv_id=conv_id):
                out.player_learned.append(npc.name)
                out.log.append(f"the player called {npc.name} by name")
                out.notes.append(f"{player.name} knows {npc.name}'s name.")
    return out


def _knows_player(npc: Person, player: Person, name: str) -> str:
    if name == player.name:
        return f"{npc.name} now knows {player.name}'s name."
    return f"{npc.name} now knows {player.name} by the name {name}, and calls them {name}."


def gossip_carries_name(db, teller: int, listener: int, subject: int, game_time: float) -> bool:
    """Gossip about the player passes on the name the teller knows them by."""
    if not fog_enabled():
        return False
    row = db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (subject,)).fetchone()
    if not row or not row[0] or known_name(db, listener, subject) is not None:
        return False
    told = known_name(db, teller, subject)
    return bool(told) and learn(db, listener, subject, told, "gossip", game_time, teller)
