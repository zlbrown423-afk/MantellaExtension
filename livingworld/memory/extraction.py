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

from livingworld.memory.settings import memory_boost_enabled
from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

# (system prompt, user prompt) -> model reply text
Complete = Callable[[str, str], "str | None"]

SYSTEM_PROMPT = """You maintain the memory of one character in Skyrim, {owner}.
You read a conversation {owner} took part in and record only what {owner} now knows or remembers.

Return JSON only, with this shape:
{{"memories": [{{"text": "<one sentence, first person, as {owner}>", "importance": <0.0 to 1.0>}}],
 "facts": [{{"about": "<name>", "predicate": "<short_snake_case>", "value": "<short value>", "said_by": "<name>", "line": <line number>}}],
 "reports": [{{"about": "<name>", "did": "<short past-tense phrase>", "valence": <-1.0 to 1.0>, "said_by": "<name>", "line": <line number>}}]}}

Rules:
- A fact is a lasting personal detail about a named person: age, hometown, family, job, likes, fears, plans, and so on.
- "line" is the number of the line where the fact was said, and "said_by" is who said that line.
- Only record facts that were actually stated. Never guess. A question is not a fact.
- "about" must be one of: {names}.
- Facts {owner} states about themselves count too (about = said_by = {owner}).
- Memories are what {owner} would remember about this conversation, at most 3. Be specific: names, places,
  promises, plans, requests, what was agreed and how {owner} felt ("Titus promised to bring me timber tomorrow",
  not "we had a conversation"). Importance: 0.2 small talk, 0.5 useful, 0.8 something {owner} will not forget.
- A report is someone other than {owner} telling {owner} what a person did: a past action, not an opinion or a plan.
  "did" continues the person's name, e.g. "helped Gerdur carry timber" or "stole a sweetroll from Lucan".
  "valence" is how well it reflects on that person (-1 very badly, 1 very well). "about" may be anyone named,
  present or not, but never {owner}. Only record reports that were actually said; at most 3.
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
    reports_added: int = 0
    facts_skipped: int = 0
    memories_added: int = 0
    rejected: list[str] = field(default_factory=list)


def normalize_predicate(predicate: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", predicate.strip().lower()).strip("_")


def parse_reply(reply: str | None) -> dict[str, Any]:
    """Pull the JSON object out of a model reply (tolerates code fences and chatter around it).

    A reply cut off part way (a length limit) or with trailing commas is mended where it can be: everything
    up to the last complete entry is kept, so one cut-off memory does not throw away the whole reply."""
    if not reply:
        raise ValueError("empty extraction reply")
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0:
        raise ValueError(f"no JSON object in extraction reply: {reply[:200]!r}")
    data = None
    if end > start:
        try:
            data = json.loads(reply[start:end + 1])
        except json.JSONDecodeError:
            data = None
    if data is None:
        data = _mend(reply[start:])
        if data is None:
            raise ValueError(f"extraction reply is not valid JSON: {reply[:200]!r}")
        logger.log(23, "Living World: mended a cut-off or malformed extraction reply")
    if not isinstance(data, dict):
        raise ValueError("extraction reply is not a JSON object")
    for key in ("facts", "memories", "reports"):
        if not isinstance(data.get(key), list):
            data[key] = []
    # some models give memories as plain sentences instead of {"text": ...}
    data["memories"] = [{"text": m} if isinstance(m, str) else m for m in data["memories"]]
    return data


def _mend(text: str) -> Any:
    """Best effort for a broken JSON object: drop trailing commas, then cut back to the last complete
    value and close whatever is still open."""
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # walk the text once, remembering where each value inside a container ended and what was open there
    stack: list[str] = []
    cuts: list[tuple[int, str]] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                try:
                    return json.loads(text[:i + 1])
                except json.JSONDecodeError:
                    break
            cuts.append((i + 1, "".join(reversed(stack))))
    for cut, closing in reversed(cuts[-60:]):
        candidate = re.sub(r",\s*([}\]])", r"\1", text[:cut] + closing)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip().strip("[]"))
    except (TypeError, ValueError):
        return None


def _line_saying(heard: list[Line], speaker_id: int, value: str, claimed: Any) -> Line | None:
    """The model gave the wrong line number: find a line by that speaker that actually holds the value
    (nearest to the line it named). Nothing is accepted that the speaker did not say."""
    words = [w for w in re.findall(r"[a-z0-9']+", value.lower()) if len(w) > 2 or w.isdigit()]
    if not words:
        return None
    near = _as_int(claimed)
    matches = [l for l in heard if l.speaker_id == speaker_id
               and all(re.search(rf"\b{re.escape(w)}", l.text.lower()) for w in words)]
    if not matches:
        return None
    return min(matches, key=lambda l: abs(l.seq - near) if near is not None else 0)


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
    def extract_for(self, conv_id: int, owner_id: int, backfill: bool = False) -> ExtractionResult:
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
        system = SYSTEM_PROMPT.format(owner=owner, names=name_list)
        user = USER_PROMPT.format(location=location, names=name_list, transcript=transcript, owner=owner)
        reply = self.complete(system, user)
        try:
            data = parse_reply(reply)
        except ValueError as e:
            if not memory_boost_enabled():
                raise
            # ask once more straight away instead of waiting for the job retry, which would get the same reply
            logger.log(23, f"Living World: extraction reply for {owner} was not usable ({e}); asking again")
            reply = self.complete(system, user + "\n\nYour last reply was not valid JSON. Reply with one short,"
                                                  " complete JSON object only, with no other text.")
            data = parse_reply(reply)
        if backfill:
            data["reports"] = []   # old news is not passed on again as if it were fresh gossip
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
                    line = by_seq.get(_as_int(fact.get("line")))
                except (TypeError, ValueError, AttributeError):
                    result.rejected.append(f"malformed: {fact}")
                    continue
                if about is None or said_by is None or not predicate or not value:
                    result.rejected.append(f"unknown person or empty field: {fact}")
                    continue
                if (line is None or line.speaker_id != said_by) and said_by is not None and memory_boost_enabled():
                    line = _line_saying(heard, said_by, value, fact.get("line"))
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
                    "SELECT know_id, value, learned_gt FROM knowledge WHERE holder_id = ? AND subject_id = ? AND predicate = ?"
                    " AND superseded_by IS NULL AND timeline_id = ?", (owner_id, about, predicate, timeline)).fetchone()
                if existing and existing[1].lower() == value.lower():
                    result.facts_skipped += 1
                    continue
                if existing and existing[2] is not None and game_time is not None and existing[2] > game_time:
                    result.facts_skipped += 1   # an older conversation (a late or backfilled job) never overrides newer news
                    continue
                cur = db.execute(
                    "INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, told_by_id,"
                    " parent_know_id, source_utt_id, learned_gt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (timeline, owner_id, about, predicate, value, origin, told_by, parent, line.utt_id, game_time))
                if existing:
                    db.execute("UPDATE knowledge SET superseded_by = ? WHERE know_id = ?", (cur.lastrowid, existing[0]))
                result.facts_added += 1

            for memory in (data.get("memories") or [])[:3]:
                text = str(memory.get("text") or memory.get("memory") or memory.get("content") or "").strip() if isinstance(memory, dict) else ""
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

        self._apply_reports(owner_id, data, by_seq, names, location, game_time, result)
        for reason in result.rejected:
            logger.log(23, f"Living World: dropped a fact for {names[owner_id]}: {reason}")
        return result

    # ---- reports about other people become claims that can spread (livingworld/social) ----
    def _apply_reports(self, owner_id: int, data: dict[str, Any], by_seq: dict[int, Line], names: dict[int, str],
                       location: str, game_time: float, result: ExtractionResult) -> None:
        from livingworld.social import claims
        from livingworld.store.minors import is_child
        by_name = {name.lower(): npc_id for npc_id, name in names.items()}
        with self.store.transaction() as db:
            timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
            if is_child(db, owner_id):
                return
            for report in (data.get("reports") or [])[:3]:
                try:
                    said_by = by_name.get(str(report.get("said_by", "")).strip().lower())
                    about_name = str(report.get("about", "")).strip()
                    did = " ".join(str(report.get("did", "")).split())[:160]
                    valence = max(-1.0, min(1.0, float(report.get("valence", 0))))
                    line = by_seq.get(int(report.get("line")))
                except (TypeError, ValueError, AttributeError):
                    result.rejected.append(f"malformed report: {report}")
                    continue
                about = by_name.get(about_name.lower()) or self._named(db, about_name)
                if said_by is None or about is None or not did:
                    result.rejected.append(f"report with unknown person or nothing said: {report}")
                    continue
                if line is None or line.speaker_id != said_by or said_by == owner_id or about == owner_id:
                    result.rejected.append(f"report not said on that line by someone else: {report}")
                    continue
                if is_child(db, about) or is_child(db, said_by):
                    continue
                if db.execute("SELECT 1 FROM v_claim_knowledge WHERE holder_id = ? AND subject_id = ? AND act = 'reported'"
                              " AND lower(detail) = lower(?)", (owner_id, about, did)).fetchone():
                    continue
                magnitude = round(0.3 + 0.5 * abs(valence), 3)
                claim_id = db.execute(
                    "INSERT INTO claim (timeline_id, subject_id, act, detail, valence, magnitude, location, game_time)"
                    " VALUES (?,?,?,?,?,?,?,?)", (timeline, about, "reported", did, valence, magnitude, location,
                                                  game_time)).lastrowid
                claim = {"claim_id": claim_id, "subject_id": about, "act": "reported", "object_id": None,
                         "valence": valence}
                claims.hear(db, timeline, owner_id, said_by, claim, None, 1.0, magnitude, game_time,
                            utt_id=line.utt_id)
                result.reports_added += 1

    def _is_child(self, npc_id: int) -> bool:
        from livingworld.store.minors import is_child
        with self.store.transaction() as db:
            return is_child(db, npc_id)

    @staticmethod
    def _named(db, name: str) -> int | None:
        """An NPC not in the conversation, by name, only if the name is unambiguous."""
        rows = db.execute("SELECT npc_id FROM npc WHERE lower(display_name) = lower(?) AND is_player = 0", (name,)).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    # ---- the whole conversation ----
    def extract_conversation(self, conv_id: int, backfill: bool = False) -> dict[str, ExtractionResult]:
        results = {}
        for npc_id, name, is_player in self.participants(conv_id):
            if is_player or self._is_child(npc_id):
                continue   # children are left out of Living World memory
            results[name] = self.extract_for(conv_id, npc_id, backfill)
            r = results[name]
            logger.log(23, f"Living World: {name} learned {r.facts_added} facts and {r.memories_added} memories"
                           f" from conversation {conv_id}")
        return results
