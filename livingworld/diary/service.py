"""The store side of diaries: who keeps one, writing a day's entry, the pages the game shows, and reads.

Everything an entry is written from is the NPC's own: their memories (``v_visible_memory``), what they were
told (``v_known_facts``) and how they feel about the player (``recall.opinion_line``). Nothing another NPC
knows leaks into it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from livingworld import diary
from livingworld.memory.recall import opinion_line
from livingworld.store.identity import parse_form_id
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore
from livingworld.work.conditions import MONTH_DAYS, MONTHS, START_DAY_OF_YEAR
from src import utils

logger = utils.get_logger()

Complete = Callable[[str, str], "str | None"]

WEEKDAYS = ("Sundas", "Morndas", "Tirdas", "Middas", "Turdas", "Fredas", "Loredas")   # 17 Last Seed 4E 201 is a Sundas
MAX_ENTRY_CHARS = 1200
PAGE_CHARS = 850          # a VR message box shows about this much without scrolling
MAX_PAGES = 6
DAYS_SHOWN = 6
HEADER = "You keep the private diary of {name}, a person living in Skyrim.\n"


# ---- dates ----

def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def date_text(game_day: int) -> str:
    """'Sundas, 17th of Last Seed' for a whole game day as the server counts it: the game sends GameDaysPassed + 1,
    so day 1 is the day a new game starts."""
    game_day -= 1
    day = (START_DAY_OF_YEAR + game_day) % 365
    month = MONTHS[-1]
    for name, length in zip(MONTHS, MONTH_DAYS):
        if day < length:
            month = name
            break
        day -= length
    return f"{WEEKDAYS[game_day % 7]}, {_ordinal(day + 1)} of {month}"


def _part_of_day(game_days: float) -> str:
    hour = int((game_days % 1.0) * 24)
    if hour < 5:
        return "in the night"
    if hour < 12:
        return "in the morning"
    if hour < 17:
        return "in the afternoon"
    if hour < 21:
        return "in the evening"
    return "late at night"


# ---- who keeps one ----

def talks_with_player(db, npc_id: int, player_id: int) -> int:
    """Conversations (on the active timeline) the NPC and the player were both in."""
    return db.execute(
        "SELECT COUNT(*) FROM conversation c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
        " AND c.started_gt <= s.cutoff_gt"
        " WHERE c.kind = 'player'"
        " AND EXISTS (SELECT 1 FROM participant p WHERE p.conv_id = c.conv_id AND p.npc_id = ?)"
        " AND EXISTS (SELECT 1 FROM participant p WHERE p.conv_id = c.conv_id AND p.npc_id = ?)",
        (npc_id, player_id)).fetchone()[0]


def is_keeper(db, npc_id: int, player_id: int | None) -> bool:
    if player_id is None or npc_id == player_id:
        return False
    row = db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row or row[0]:
        return False
    if is_child(db, npc_id):
        return False
    return talks_with_player(db, npc_id, player_id) >= diary.min_talks()


def player_id(db) -> int | None:
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
    return row[0] if row else None


def title(db, npc_id: int, player: int | None, fog_on: bool) -> str:
    """The book's name. With fog on, someone the player does not know by name gets their description."""
    row = db.execute("SELECT display_name, stranger_label FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row:
        return "Diary"
    name, label = row[0], row[1]
    if fog_on and player is not None and label:
        known = db.execute("SELECT 1 FROM v_name_known WHERE knower_id = ? AND subject_id = ?",
                           (player, npc_id)).fetchone()
        if not known:
            return f"{label[0].upper()}{label[1:]}'s Diary"
    return f"{name}'s Diary"


# ---- writing ----

@dataclass
class Notes:
    """What the writer is shown for one day."""
    name: str
    player_name: str
    day: int
    memories: list[str]
    heard: list[str]
    opinion: str | None
    today_so_far: str | None
    previous: str | None
    first: bool
    last_mem_id: int | None

    def has_news(self) -> bool:
        return bool(self.memories or self.heard)


def _notes(store: WorldStore, npc_id: int, player: int, day: int, since_mem_id: int | None) -> Notes | None:
    with store.reading() as db:
        row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
        prow = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (player,)).fetchone()
        if not row or not prow:
            return None
        newest = db.execute("SELECT MAX(mem_id) FROM v_visible_memory WHERE owner_id = ?", (npc_id,)).fetchone()[0]
        if since_mem_id is not None and (newest is None or newest <= since_mem_id):
            return None   # nothing new since this day's entry was last written
        earlier = db.execute("SELECT game_day, text FROM v_diary_entry WHERE npc_id = ? AND game_day < ?"
                             " ORDER BY game_day DESC LIMIT 1", (npc_id, day)).fetchone()
        today = db.execute("SELECT text FROM v_diary_entry WHERE npc_id = ? AND game_day = ?",
                           (npc_id, day)).fetchone()
        first = earlier is None and today is None
        if first:   # the first entry looks back over everything that mattered so far
            mems = db.execute(
                "SELECT content, occurred_gt FROM v_visible_memory WHERE owner_id = ? AND occurred_gt < ?"
                " ORDER BY importance DESC, occurred_gt DESC LIMIT 12", (npc_id, day + 1)).fetchall()
        else:
            mems = db.execute(
                "SELECT content, occurred_gt FROM v_visible_memory WHERE owner_id = ? AND occurred_gt >= ?"
                " AND occurred_gt < ? ORDER BY importance DESC, occurred_gt DESC LIMIT 12",
                (npc_id, day, day + 1)).fetchall()
        memories = []
        for content, occurred in sorted(mems, key=lambda r: r[1]):
            when = _part_of_day(occurred) if int(occurred) == day else f"on {date_text(int(occurred))}"
            memories.append(f"({when}) {content}")
        heard = [r[0] for r in db.execute(
            "SELECT prompt_line FROM v_known_facts WHERE holder_id = ? AND origin IN ('told','overheard','rumor')"
            " AND predicate NOT LIKE 'claim:%' AND learned_gt >= ? AND learned_gt < ?"
            " ORDER BY learned_gt DESC LIMIT 6", (npc_id, day if not first else 0, day + 1)).fetchall()]
    return Notes(name=row[0], player_name=prow[0], day=day, memories=memories, heard=heard,
                 opinion=opinion_line(store, npc_id, player), today_so_far=today[0] if today else None,
                 previous=earlier[1] if earlier else None, first=first, last_mem_id=newest)


def build_prompt(notes: Notes, bio: str | None) -> tuple[str, str]:
    system = (HEADER.format(name=notes.name) +
              f"Write {notes.name}'s diary entry for the day, in the first person, in {notes.name}'s own voice "
              f"and manner. It is private: {notes.name} writes what they really think and feel, including things "
              f"they would never say out loud, about the people in their life and about {notes.player_name}. "
              "Write only about what is in the notes; do not invent events, people or places. Keep it plain and "
              "true to Skyrim: no modern words. 60 to 160 words, no date line, no greeting, no signature.\n"
              "If an entry for today already exists, rewrite it so it also covers the new things, keeping what "
              "still matters.\n"
              'Reply with JSON only: {"entry": "<the diary entry>"}')
    parts = []
    if bio:
        parts.append(f"About {notes.name}: {bio.strip()[:700]}")
    parts.append(f"Today is {date_text(notes.day)}.")
    if notes.first:
        parts.append(f"This is the first entry in which {notes.name} writes about {notes.player_name}; "
                     f"look back briefly on how they came into {notes.name}'s life.")
    if notes.opinion:
        parts.append(notes.opinion.replace("let it show in my manner rather than saying it outright",
                                           "in the diary it can be said plainly"))
    if notes.memories:
        parts.append("What I remember (my own memories):\n" + "\n".join(f"- {m}" for m in notes.memories))
    if notes.heard:
        parts.append("What I heard from others:\n" + "\n".join(f"- {h}" for h in notes.heard))
    if notes.today_so_far:
        parts.append(f"Today's entry so far:\n{notes.today_so_far}")
    elif notes.previous:
        parts.append(f"My last entry (do not repeat it):\n{notes.previous[:500]}")
    return system, "\n\n".join(parts)


def parse_entry(reply: str | None) -> str | None:
    if not reply:
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            text = str(data.get("entry") or "").strip()
        elif isinstance(data, str):
            text = data.strip()
    except ValueError:
        m = re.search(r'"entry"\s*:\s*"(.*)"\s*}?\s*$', text, re.S)
        if m:
            text = m.group(1).replace('\\"', '"').replace("\\n", "\n").strip()
    text = text.strip().strip('"').strip()
    if len(text) < 20:
        return None
    if len(text) > MAX_ENTRY_CHARS:
        cut = text[:MAX_ENTRY_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:end + 1] if end > 200 else cut
    return text


def write_entry(store: WorldStore, complete: Complete, payload: dict[str, Any]) -> str | None:
    """The diary job: (re)write the keeper's entry for the day. Returns the text, or None when skipped."""
    npc_id, player, day = int(payload["npc_id"]), int(payload["player_id"]), int(payload["day"])
    with store.reading() as db:
        if not is_keeper(db, npc_id, player):
            return None
        row = db.execute("SELECT versions, last_mem_id FROM v_diary_entry WHERE npc_id = ? AND game_day = ?",
                         (npc_id, day)).fetchone()
    if row and row[0] >= diary.max_rewrites():
        logger.log(23, f"Living World diary: npc {npc_id} already wrote {row[0]} versions of day {day}; skipped")
        return None
    notes = _notes(store, npc_id, player, day, row[1] if row else None)
    if notes is None or not notes.has_news():
        return None
    system, user = build_prompt(notes, payload.get("bio"))
    text = parse_entry(complete(system, user))
    if text is None:
        raise ValueError("the diary reply held no entry")
    with store.transaction() as db:
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        db.execute("INSERT INTO diary_entry (timeline_id, npc_id, game_day, text, last_mem_id, conv_id, written_gt)"
                   " VALUES (?,?,?,?,?,?,?)",
                   (timeline, npc_id, day, text, notes.last_mem_id, payload.get("conv_id"),
                    float(payload.get("game_days") or day)))
    logger.log(23, f"Living World diary: {notes.name} wrote about {date_text(day)} ({len(text)} characters)")
    return text


# ---- reading ----

def _split(text: str, size: int = PAGE_CHARS) -> list[str]:
    pages, rest = [], text.strip()
    while len(rest) > size:
        cut = rest[:size]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        end = end + 1 if end > size // 3 else cut.rfind(" ")
        if end <= 0:
            end = size
        pages.append(rest[:end].strip())
        rest = rest[end:].strip()
    if rest:
        pages.append(rest)
    return pages


def pages(db, npc_id: int) -> list[str]:
    """The newest entries, newest first, split into pages that fit a VR message box."""
    rows = db.execute("SELECT game_day, text FROM v_diary_entry WHERE npc_id = ? ORDER BY game_day DESC LIMIT ?",
                      (npc_id, DAYS_SHOWN)).fetchall()
    out: list[str] = []
    for day, text in rows:
        parts = _split(text, PAGE_CHARS - 40)
        for i, part in enumerate(parts):
            head = date_text(day) if i == 0 else f"{date_text(day)} (continued)"
            out.append(f"{head}\n\n{part}")
            if len(out) >= MAX_PAGES:
                return out
    return out


def _witness_ids(db, ref_ids: list[Any], names: list[Any], owner_id: int, player: int) -> list[int]:
    found = []
    for i, raw in enumerate(ref_ids or []):
        try:
            local = parse_form_id(raw).local_id
        except (TypeError, ValueError):
            continue
        name = str(names[i]) if i < len(names or []) else None
        # ref_form_id is 'plugin|0x00013489'; the game sends the runtime ID, whose plugin part needs the load order
        pattern = "%|" + local
        row = db.execute("SELECT npc_id FROM npc WHERE ref_form_id LIKE ? AND is_player = 0"
                         + (" AND display_name = ?" if name else "") + " ORDER BY npc_id LIMIT 1",
                         (pattern, name) if name else (pattern,)).fetchone()
        if row and row[0] not in (owner_id, player) and row[0] not in found and not is_child(db, row[0]):
            found.append(row[0])
    return found[:6]


def record_read(store: WorldStore, owner_id: int, player: int, seen: bool, witness_refs: list[Any],
                witness_names: list[Any], location: str | None, game_time: float) -> list[int]:
    """Store that the player read the diary; the owner (if they saw) and witnesses remember it.
    Returns the npc ids who now remember it."""
    remembered: list[int] = []
    with store.transaction() as db:
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        names = dict(db.execute("SELECT npc_id, display_name FROM npc WHERE npc_id IN (?,?)", (owner_id, player)))
        owner, reader = names.get(owner_id, "someone"), names.get(player, "the stranger")
        witnesses = _witness_ids(db, witness_refs, witness_names, owner_id, player)
        event_id = None
        if seen or witnesses:
            event_id = db.execute(
                "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
                " VALUES (?,?,?,?,?,?,?)",
                (timeline, "diary.read", location, json.dumps([player, owner_id]),
                 json.dumps({"seen_by_owner": bool(seen)}), "game", game_time)).lastrowid
            for npc_id in ([owner_id] if seen else []) + witnesses:
                db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
                           (event_id, npc_id, "saw"))
        db.execute("INSERT INTO diary_read (timeline_id, owner_id, reader_id, seen_by_owner, event_id, location,"
                   " game_time) VALUES (?,?,?,?,?,?,?)",
                   (timeline, owner_id, player, int(bool(seen)), event_id, location, game_time))
        if seen:
            _remember(db, timeline, owner_id, event_id, location, game_time,
                      f"I caught {reader} reading my diary. Those pages are private; they had no right.",
                      [player], importance=0.85, valence=-0.8, intensity=0.8,
                      deltas={"trust": -0.25, "affinity": -0.15, "suspicion": 0.25}, to_id=player,
                      reason=f"caught {reader} reading my diary")
            remembered.append(owner_id)
        for npc_id in witnesses:
            _remember(db, timeline, npc_id, event_id, location, game_time,
                      f"I saw {reader} reading {owner}'s diary.", [player, owner_id],
                      importance=0.5, valence=-0.3, intensity=0.4,
                      deltas={"suspicion": 0.08, "respect": -0.05}, to_id=player,
                      reason=f"saw {reader} reading {owner}'s diary")
            remembered.append(npc_id)
    logger.log(23, f"Living World diary: {reader} read {owner}'s diary"
                   f"{' (caught by them)' if seen else ''}; remembered by {len(remembered)} NPC(s)")
    return remembered


def _remember(db, timeline: int, npc_id: int, event_id: int | None, location: str | None, gt: float, content: str,
              about: list[int], importance: float, valence: float, intensity: float, deltas: dict[str, float],
              to_id: int, reason: str) -> None:
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
        " importance, valence, intensity, created_by, source_kind, event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (timeline, npc_id, "episodic", content, json.dumps(about), location, gt, importance, valence, intensity,
         "perception", "witnessed", event_id)).lastrowid
    if event_id is not None:
        db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
               (npc_id, to_id, "stranger", gt))
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_mem_id,"
               " cause_event_id, reason, game_time) VALUES (?,?,?,?,?,?,?,?)",
               (timeline, npc_id, to_id, json.dumps(deltas), mem_id, event_id, reason, gt))
