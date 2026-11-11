"""Recall: the lines of one NPC's own knowledge and memories that go into that NPC's prompt.

Reads only through the timeline-aware views (``v_known_facts``, ``v_visible_memory``) and only
rows the NPC holds or owns, so nothing another NPC knows can reach this prompt.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from livingworld.memory.settings import memory_boost_enabled
from livingworld.store.world_store import WorldStore

MAX_FACTS = 40
MAX_MEMORIES = 8
MAX_HEARD = 3
MAX_SEEN = 3
SEEN_DAYS = 30.0   # things they saw that left no memory row (village life: flirting, couples) stay this long
FRESH_DAYS = 3.0   # a perceived event this recent, never yet recalled, may be raised unprompted
# with MANTELLA_LW_MEMORY_BOOST on (the default):
TALK_SLOTS = 6        # memories of what was said, kept apart so a run of grabs cannot push them all out
PERCEIVED_SLOTS = 6   # things they saw or went through; repeats of the same thing are shown once
RECAP_LINES = 8       # the end of the last conversation with the player, word for word
RECAP_DAYS = 2.0      # ... if it ended this recently (game days)
RECAP_CHARS = 220


def time_ago(days: float) -> str:
    if days < 0.04:
        return "just now"
    if days < 1:
        hours = round(days * 24)
        return f"about {hours} hour{'s' if hours != 1 else ''} ago"
    if days < 1.5:
        return "yesterday"
    if days < 13:
        return f"about {round(days)} days ago"
    if days < 60:
        weeks = round(days / 7)
        return f"about {weeks} week{'s' if weeks != 1 else ''} ago"
    months = round(days / 30)
    return f"about {months} month{'s' if months != 1 else ''} ago"


@dataclass
class Recall:
    npc_id: int
    name: str
    facts: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    know_ids: list[int] = field(default_factory=list)
    mem_ids: list[int] = field(default_factory=list)
    last_spoke: str | None = None
    opinion: str | None = None                       # how they feel about the player right now
    heard: list[str] = field(default_factory=list)  # news other people told them
    seen: list[str] = field(default_factory=list)   # village life they saw or took part in (no memory row)
    romance: list[str] = field(default_factory=list)  # their own love life (social/romance.py)
    beliefs: list[str] = field(default_factory=list)  # opinions they have formed (social/reputation.py)
    fresh_perceptions: int = 0   # things they saw or went through recently and have not had a chance to raise
    recap: list[str] = field(default_factory=list)   # how the last conversation with the player ended

    def text(self) -> str:
        if not (self.facts or self.memories or self.last_spoke or self.opinion or self.heard or self.seen
                or self.romance or self.beliefs or self.recap):
            return ""
        parts = [f"What {self.name} knows and remembers (only {self.name} knows this; "
                 f"anything not listed here, {self.name} does not know):"]
        if self.last_spoke:
            parts.append(f"- {self.last_spoke}")
        parts += [f"- {line}" for line in self.facts]
        parts += [f"- {line}" for line in self.memories]
        parts += [f"- {line}" for line in self.seen]
        parts += [f"- {line}" for line in self.heard]
        parts += [f"- {line}" for line in self.romance]
        parts += [f"- {line}" for line in self.beliefs]
        if self.opinion:
            parts.append(f"- {self.opinion}")
        if self.recap:
            parts += self.recap
        if self.fresh_perceptions:
            parts.append(f"- {self.name} may bring up something they recently saw or went through without being asked, "
                         f"if it fits the moment. {self.name} only knows what they perceived, not why it happened.")
        return "\n".join(parts)


def recall_for(store: WorldStore, npc_id: int, now_gt: float, player_id: int | None = None,
               mark_rehearsed: bool = True, about_ids: list[int] | None = None,
               current_conv_id: int | None = None) -> Recall:
    name = store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,))
    recall = Recall(npc_id, name)

    facts = store.query(
        "SELECT know_id, prompt_line FROM v_known_facts WHERE holder_id = ? AND predicate NOT LIKE 'claim:%' ORDER BY learned_gt DESC, know_id DESC LIMIT ?",
        (npc_id, MAX_FACTS))
    for know_id, line in reversed(facts):
        recall.know_ids.append(know_id)
        recall.facts.append(line)

    if memory_boost_enabled():
        _pick_memories(store, recall, now_gt)
    else:
        memories = store.query(
            "SELECT mem_id, content, occurred_gt, source_kind, interpretation, rehearsal_count, importance"
            " FROM v_visible_memory WHERE owner_id = ?"
            " ORDER BY occurred_gt DESC, importance DESC LIMIT ?", (npc_id, MAX_MEMORIES))
        for mem_id, content, occurred, source_kind, interpretation, rehearsals, importance in reversed(memories):
            _add_memory(recall, now_gt, mem_id, content, occurred, source_kind, interpretation, rehearsals, importance)

    if player_id is not None:
        last = store.scalar(
            "SELECT MAX(c.started_gt) FROM conversation c"
            " JOIN participant a ON a.conv_id = c.conv_id AND a.npc_id = ?"
            " JOIN participant b ON b.conv_id = c.conv_id AND b.npc_id = ?"
            " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt"
            " WHERE c.started_gt < ?", (npc_id, player_id, now_gt - 0.001))
        if last is not None:
            player = store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (player_id,))
            recall.last_spoke = f"I last spoke with {player} {time_ago(now_gt - last)}."
        if memory_boost_enabled():
            _recap(store, recall, player_id, now_gt, current_conv_id)

    _seen(store, recall, now_gt)
    _heard(store, recall, now_gt)
    from livingworld.social import romance
    if romance.romance_enabled():
        with store.reading() as db:
            recall.romance = romance.recall_lines(db, npc_id, now_gt)
    if player_id is not None:
        recall.opinion = opinion_line(store, npc_id, player_id)
    about = ([player_id] if player_id is not None else []) + [i for i in (about_ids or []) if i not in (npc_id, player_id)]
    if about:
        from livingworld.social import reputation
        with store.reading() as db:
            recall.beliefs = reputation.belief_lines(db, npc_id, about)

    if mark_rehearsed and recall.mem_ids:
        with store.transaction() as db:
            db.executemany("UPDATE memory SET rehearsal_count = rehearsal_count + 1, last_recalled_gt = ? WHERE mem_id = ?",
                           [(now_gt, m) for m in recall.mem_ids])
    return recall


def _add_memory(recall: Recall, now_gt: float, mem_id: int, content: str, occurred: float, source_kind: str | None,
                interpretation: str | None, rehearsals: int, importance: float, times: int = 1) -> None:
    recall.mem_ids.append(mem_id)
    how = {"experienced": ", it happened to me", "witnessed": ", I saw it myself"}.get(source_kind or "", "")
    when = time_ago(now_gt - occurred)
    if times > 1:
        when = f"{times} times, most recently {when}"
    line = f"({when}{how}) {content}"
    if interpretation:
        line += f" What I made of it: {interpretation}"
    recall.memories.append(line)
    if source_kind in ("experienced", "witnessed") and rehearsals == 0 and importance >= 0.4 \
            and now_gt - occurred <= FRESH_DAYS:
        recall.fresh_perceptions += 1


def _pick_memories(store: WorldStore, recall: Recall, now_gt: float) -> None:
    """The memories worth the prompt space, not just the newest ones.

    What was said (extraction memories) and what was seen or done (perception memories) get their own
    slots, so fifty shoulder grabs cannot hide the promise made yesterday. The same memory repeated is shown
    once with a count. Within each kind, importance and recency both count."""
    rows = store.query(
        "SELECT mem_id, content, occurred_gt, source_kind, interpretation, rehearsal_count, importance, created_by"
        " FROM v_visible_memory WHERE owner_id = ? AND occurred_gt <= ?"
        " ORDER BY occurred_gt DESC, mem_id DESC LIMIT 400", (recall.npc_id, now_gt + 1e-9))
    groups: dict[str, dict] = {}
    for mem_id, content, occurred, source_kind, interpretation, rehearsals, importance, created_by in rows:
        key = re.sub(r"\d+", "#", " ".join(content.lower().split()))   # "held me for 27 seconds" ~ "... 34 seconds"
        group = groups.get(key)
        if group is None:   # rows come newest first, so the first one is the newest
            groups[key] = {"mem_id": mem_id, "content": content, "occurred": occurred, "source_kind": source_kind,
                           "interpretation": interpretation, "rehearsals": rehearsals,
                           "importance": importance or 0.0, "times": 1,
                           "perceived": source_kind in ("experienced", "witnessed") or created_by == "perception"}
        else:
            group["times"] += 1
            group["importance"] = max(group["importance"], importance or 0.0)

    def score(g: dict) -> float:
        age = max(0.0, now_gt - g["occurred"])
        return g["importance"] + 0.6 * math.exp(-age / 3.0) + 0.05 * min(g["times"] - 1, 4)

    talk = sorted((g for g in groups.values() if not g["perceived"]), key=score, reverse=True)[:TALK_SLOTS]
    seen = sorted((g for g in groups.values() if g["perceived"]), key=score, reverse=True)[:PERCEIVED_SLOTS]
    for g in sorted(talk + seen, key=lambda g: (g["occurred"], g["mem_id"])):
        _add_memory(recall, now_gt, g["mem_id"], g["content"], g["occurred"], g["source_kind"], g["interpretation"],
                    g["rehearsals"], g["importance"], g["times"])


def _recap(store: WorldStore, recall: Recall, player_id: int, now_gt: float, current_conv_id: int | None) -> None:
    """How the last conversation with the player ended, in their own words, so a conversation started again
    picks up where it stopped. Works straight away, before the background extraction has run."""
    row = store.query(
        "SELECT c.conv_id, COALESCE(c.ended_gt, c.started_gt), c.location, a.joined_seq, a.left_seq"
        " FROM conversation c"
        " JOIN participant a ON a.conv_id = c.conv_id AND a.npc_id = ?"
        " JOIN participant b ON b.conv_id = c.conv_id AND b.npc_id = ?"
        " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt"
        " WHERE c.conv_id != ? AND c.started_gt <= ?"
        " AND EXISTS (SELECT 1 FROM utterance u WHERE u.conv_id = c.conv_id)"
        " ORDER BY c.started_gt DESC, c.conv_id DESC LIMIT 1",
        (recall.npc_id, player_id, current_conv_id if current_conv_id is not None else -1, now_gt + 1e-9))
    if not row:
        return
    conv_id, ended, location, joined, left = row[0]
    if now_gt - ended > RECAP_DAYS:
        return
    lines = store.query(
        "SELECT n.display_name, u.text FROM utterance u JOIN npc n ON n.npc_id = u.speaker_id"
        " WHERE u.conv_id = ? AND u.seq > ? AND (? IS NULL OR u.seq <= ?)"
        " ORDER BY u.seq DESC LIMIT ?", (conv_id, joined or 0, left, left, RECAP_LINES))
    if not lines:
        return
    player = store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (player_id,))
    where = f", in {location}" if location else ""
    recall.recap.append(f"The last time I talked with {player} ({time_ago(now_gt - ended)}{where}), "
                        f"it ended like this (pick up from here if it fits; do not repeat it word for word):")
    for speaker, text in reversed(lines):
        text = " ".join((text or "").split())
        if len(text) > RECAP_CHARS:
            text = text[:RECAP_CHARS].rsplit(" ", 1)[0] + "..."
        speaker = f"{speaker} (me)" if speaker == recall.name else speaker
        recall.recap.append(f"    {speaker}: {text}")


# ---- reputation and gossip (livingworld/social) ----

def doubt(believed: float) -> str:
    if believed >= 0.75:
        return "I believe it."
    if believed >= 0.5:
        return "It's probably true."
    return "I'm not sure I believe it."


def _heard(store: WorldStore, recall: Recall, now_gt: float) -> None:
    from livingworld.social.claims import sentence
    rows = store.query(
        "SELECT k.*, t.display_name AS teller FROM v_claim_knowledge k JOIN npc t ON t.npc_id = k.told_by_id"
        " WHERE k.holder_id = ? AND k.origin = 'told' AND k.learned_gt <= ?"
        " ORDER BY k.learned_gt DESC, k.know_id DESC LIMIT ?", (recall.npc_id, now_gt + 1e-9, MAX_HEARD))
    if not rows:
        return
    with store.reading() as db:
        for row in reversed(rows):
            claim = dict(row)
            text = sentence(db, claim, recall.npc_id)
            if claim["told_by_id"] == claim["subject_id"]:
                text = "they" + text[len(row["teller"]):]   # "Torvar told me that they helped Gerdur"
            recall.know_ids.append(row["know_id"])
            recall.heard.append(f"({time_ago(now_gt - row['learned_gt'])}) {row['teller']} told me that {text}. "
                                f"{doubt(row['believed'] or 0.0)} I did not see it myself.")


def _seen(store: WorldStore, recall: Recall, now_gt: float) -> None:
    """Claims this NPC holds because they were there, when the event left them no memory of their own.

    Perceived events (a grab, a drink) already give each witness a memory, so those are skipped. Events the
    village simulation makes up (Hod flirting with Hilde) only give the witnesses a claim, which they pass on
    as gossip; without this, the one person the player could ask about it would know nothing."""
    import json

    from livingworld.social.claims import sentence
    rows = store.query(
        "SELECT k.*, e.type AS event_type, e.payload_json FROM v_claim_knowledge k"
        " JOIN world_event e ON e.event_id = k.truth_event_id"
        " WHERE k.holder_id = ? AND k.origin = 'witnessed' AND k.claim_gt <= ? AND k.claim_gt > ?"
        " AND NOT EXISTS (SELECT 1 FROM v_visible_memory m WHERE m.owner_id = k.holder_id"
        "                 AND m.event_id = k.truth_event_id)"
        " ORDER BY k.claim_gt DESC, k.know_id DESC LIMIT ?",
        (recall.npc_id, now_gt + 1e-9, now_gt - SEEN_DAYS, MAX_SEEN))
    if not rows:
        return
    with store.reading() as db:
        for row in reversed(rows):
            claim = dict(row)
            text = sentence(db, claim, recall.npc_id)
            text = text[0].upper() + text[1:]
            involved = recall.npc_id in (claim["subject_id"], claim["object_id"])
            how = "it happened to me" if involved else "I saw it myself"
            if claim["event_type"] == "romance.flirt" and claim["object_id"] == recall.npc_id:
                returned = json.loads(claim["payload_json"] or "{}").get("returned")
                text += ". I flirted back" if returned else ". I did not return it"
            recall.know_ids.append(row["know_id"])
            recall.seen.append(f"({time_ago(now_gt - claim['claim_gt'])}, {how}) {text}.")


def opinion_line(store: WorldStore, npc_id: int, player_id: int) -> str | None:
    """How this NPC feels about the player, from their own relationship values, in plain words."""
    row = store.query("SELECT r.trust, r.affinity, r.respect, r.admiration, r.fear, r.suspicion, p.display_name"
                      " FROM v_relationship r JOIN npc p ON p.npc_id = r.to_id WHERE r.from_id = ? AND r.to_id = ?",
                      (npc_id, player_id))
    if not row:
        return None
    trust, affinity, respect, admiration, fear, suspicion, name = row[0]
    feelings = []
    if trust >= 0.3:
        feelings.append("I trust them")
    elif trust <= -0.3:
        feelings.append("I don't trust them")
    elif trust <= -0.04:
        feelings.append("I'm a little wary of them")
    if affinity >= 0.3:
        feelings.append("I like them")
    elif affinity <= -0.3:
        feelings.append("I dislike them")
    if respect >= 0.3:
        feelings.append("I respect them")
    elif respect <= -0.3:
        feelings.append("I don't think much of them")
    if admiration >= 0.1:
        feelings.append("I admire what I've heard of them")
    if fear >= 0.3:
        feelings.append("I'm afraid of them")
    elif fear >= 0.05:
        feelings.append("they make me a little uneasy")
    if suspicion >= 0.3:
        feelings.append("I'm suspicious of them")
    elif suspicion >= 0.04:
        feelings.append("I'm keeping an eye on them")
    if not feelings:
        return None
    return (f"How I feel about {name} right now: {', '.join(feelings)}. This comes from what I have seen and heard; "
            f"let it show in my manner rather than saying it outright.")
