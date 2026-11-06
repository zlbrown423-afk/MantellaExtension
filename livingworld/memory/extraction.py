"""Per-NPC memory extraction: after a conversation, each NPC who took part gets their own facts
and memories, written from their own point of view.

One AI call per NPC, never one shared summary, so Hod's rows can only hold what Hod heard.
Every fact the model returns must point at a real line in the conversation, said by the person
the model claims said it; anything else is dropped. That is what stops invented facts (asking
Gerdur for the player's sister's name must not create a "sister" row).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

# (system prompt, user prompt) -> model reply text
Complete = Callable[[str, str], "str | None"]

SYSTEM_PROMPT = """You maintain the memory of one character in Skyrim, {owner}.
You read a conversation {owner} took part in and record only what {owner} now knows or remembers.

Return JSON only, with this shape:
{{"facts": [{{"about": "<name>", "predicate": "<short_snake_case>", "value": "<short value>", "said_by": "<name>", "line": <line number>}}],
 "memories": [{{"text": "<one sentence, first person, as {owner}>", "importance": <0.0 to 1.0>}}]}}

Rules:
- A fact is a lasting personal detail about a named person: age, hometown, family, job, likes, fears, plans, and so on.
- "line" is the number of the line where the fact was said, and "said_by" is who said that line.
- Only record facts that were actually stated. Never guess. A question is not a fact.
- "about" must be one of: {names}.
- Facts {owner} states about themselves count too (about = said_by = {owner}).
- Memories are what {owner} would remember about this conversation, at most 3.
"""

USER_PROMPT = """Conversation at {location}. People present: {names}.

{transcript}

Return the JSON for {owner}."""


@dataclass
class Line:
    seq: int
    utt_id: int
    speaker_id: int
    speaker: str
    text: str


@dataclass
class ExtractionResult:
    facts_added: int = 0
    facts_skipped: int = 0
    memories_added: int = 0
    rejected: list[str] = field(default_factory=list)


def normalize_predicate(predicate: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", predicate.strip().lower()).strip("_")


def parse_reply(reply: str | None) -> dict[str, Any]:
    """Pull the JSON object out of a model reply (tolerates code fences and chatter around it)."""
    if not reply:
        raise ValueError("empty extraction reply")
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in extraction reply: {reply[:200]!r}")
    data = json.loads(reply[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("extraction reply is not a JSON object")
    data.setdefault("facts", [])
    data.setdefault("memories", [])
    return data


class Extractor:
    def __init__(self, store: WorldStore, complete: Complete) -> None:
        self.store = store
        self.complete = complete

    # ---- reading the conversation ----
    def lines(self, conv_id: int) -> list[Line]:
        rows = self.store.query(
            "SELECT u.seq, u.utt_id, u.speaker_id, n.display_name, u.text FROM utterance u"
            " JOIN npc n ON n.npc_id = u.speaker_id WHERE u.conv_id = ? ORDER BY u.seq", (conv_id,))
        return [Line(*row) for row in rows]

    def participants(self, conv_id: int) -> list[tuple[int, str, bool]]:
        rows = self.store.query(
            "SELECT n.npc_id, n.display_name, n.is_player FROM participant p JOIN npc n ON n.npc_id = p.npc_id"
            " WHERE p.conv_id = ? ORDER BY p.joined_seq, n.npc_id", (conv_id,))
        return [(r[0], r[1], bool(r[2])) for r in rows]

    def heard_by(self, conv_id: int, npc_id: int, lines: list[Line]) -> list[Line]:
        """Only the lines spoken while this NPC was in the conversation."""
        row = self.store.query("SELECT joined_seq, left_seq FROM participant WHERE conv_id = ? AND npc_id = ?",
                               (conv_id, npc_id))
        joined, left = row[0]
        return [l for l in lines if l.seq > joined and (left is None or l.seq <= left)]

    # ---- one NPC ----
    def extract_for(self, conv_id: int, owner_id: int) -> ExtractionResult:
        all_lines = self.lines(conv_id)
        people = self.participants(conv_id)
        names = {npc_id: name for npc_id, name, _ in people}
        owner = names[owner_id]
        heard = self.heard_by(conv_id, owner_id, all_lines)
        if not heard:
            return ExtractionResult()
        location, game_time = self.store.query(
            "SELECT location, COALESCE(ended_gt, started_gt) FROM conversation WHERE conv_id = ?", (conv_id,))[0]

        transcript = "\n".join(f"[{l.seq}] {l.speaker}: {l.text}" for l in heard)
        name_list = ", ".join(names.values())
        reply = self.complete(
            SYSTEM_PROMPT.format(owner=owner, names=name_list),
            USER_PROMPT.format(location=location, names=name_list, transcript=transcript, owner=owner),
        )
        data = parse_reply(reply)
        return self.apply(conv_id, owner_id, data, heard, names, location, game_time)

    # ---- writing what the model returned, after checking it ----
    def apply(self, conv_id: int, owner_id: int, data: dict[str, Any], heard: list[Line],
              names: dict[int, str], location: str, game_time: float) -> ExtractionResult:
        result = ExtractionResult()
        by_name = {name.lower(): npc_id for npc_id, name in names.items()}
        by_seq = {l.seq: l for l in heard}

        with self.store.transaction() as db:
            timeline = db.execute("SELECT timeline_id FROM conversation WHERE conv_id = ?", (conv_id,)).fetchone()[0]
            for fact in data.get("facts") or []:
                try:
                    about = by_name.get(str(fact.get("about", "")).strip().lower())
                    said_by = by_name.get(str(fact.get("said_by", "")).strip().lower())
                    predicate = normalize_predicate(str(fact.get("predicate", "")))
                    value = str(fact.get("value", "")).strip()
                    line = by_seq.get(int(fact.get("line")))
                except (TypeError, ValueError):
                    result.rejected.append(f"malformed: {fact}")
                    continue
                if about is None or said_by is None or not predicate or not value:
                    result.rejected.append(f"unknown person or empty field: {fact}")
                    continue
                if line is None or line.speaker_id != said_by:
                    result.rejected.append(f"not said on that line by {fact.get('said_by')}: {fact}")
                    continue
                if said_by == owner_id and about != owner_id:
                    # the owner telling others something: nothing new for the owner to learn
                    result.facts_skipped += 1
                    continue
                origin = "self" if about == owner_id else "told"
                told_by = None if origin == "self" else said_by
                parent = None
                if told_by is not None and told_by != about:
                    # second-hand: link to the teller's own copy, if they have one
                    row = db.execute(
                        "SELECT know_id FROM knowledge WHERE holder_id = ? AND subject_id = ? AND predicate = ?"
                        " AND superseded_by IS NULL ORDER BY know_id DESC LIMIT 1",
                        (told_by, about, predicate)).fetchone()
                    parent = row[0] if row else None
                existing = db.execute(
                    "SELECT know_id, value FROM knowledge WHERE holder_id = ? AND subject_id = ? AND predicate = ?"
                    " AND superseded_by IS NULL AND timeline_id = ?", (owner_id, about, predicate, timeline)).fetchone()
                if existing and existing[1].lower() == value.lower():
                    result.facts_skipped += 1
                    continue
                cur = db.execute(
                    "INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, told_by_id,"
                    " parent_know_id, source_utt_id, learned_gt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (timeline, owner_id, about, predicate, value, origin, told_by, parent, line.utt_id, game_time))
                if existing:
                    db.execute("UPDATE knowledge SET superseded_by = ? WHERE know_id = ?", (cur.lastrowid, existing[0]))
                result.facts_added += 1

            for memory in (data.get("memories") or [])[:3]:
                text = str(memory.get("text", "")).strip() if isinstance(memory, dict) else ""
                if not text:
                    continue
                duplicate = db.execute(
                    "SELECT 1 FROM memory m JOIN memory_source s ON s.mem_id = m.mem_id JOIN utterance u ON u.utt_id = s.utt_id"
                    " WHERE m.owner_id = ? AND m.content = ? AND u.conv_id = ?", (owner_id, text, conv_id)).fetchone()
                if duplicate:
                    continue
                try:
                    importance = min(1.0, max(0.0, float(memory.get("importance", 0.5))))
                except (TypeError, ValueError):
                    importance = 0.5
                about_ids = sorted({l.speaker_id for l in heard if l.speaker_id != owner_id})
                cur = db.execute(
                    "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                    " importance, created_by) VALUES (?,?,?,?,?,?,?,?,'extraction')",
                    (timeline, owner_id, "episodic", text, json.dumps(about_ids), location, game_time, importance))
                db.execute("INSERT INTO memory_source (mem_id, utt_id) VALUES (?, ?)", (cur.lastrowid, heard[-1].utt_id))
                result.memories_added += 1

        for reason in result.rejected:
            logger.log(23, f"Living World: dropped a fact for {names[owner_id]}: {reason}")
        return result

    # ---- the whole conversation ----
    def extract_conversation(self, conv_id: int) -> dict[str, ExtractionResult]:
        results = {}
        for npc_id, name, is_player in self.participants(conv_id):
            if is_player:
                continue
            results[name] = self.extract_for(conv_id, npc_id)
            r = results[name]
            logger.log(23, f"Living World: {name} learned {r.facts_added} facts and {r.memories_added} memories"
                           f" from conversation {conv_id}")
        return results
