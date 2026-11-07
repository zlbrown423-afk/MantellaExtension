"""Recall: the lines of one NPC's own knowledge and memories that go into that NPC's prompt.

Reads only through the timeline-aware views (``v_known_facts``, ``v_visible_memory``) and only
rows the NPC holds or owns, so nothing another NPC knows can reach this prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.store.world_store import WorldStore

MAX_FACTS = 40
MAX_MEMORIES = 8
FRESH_DAYS = 3.0   # a perceived event this recent, never yet recalled, may be raised unprompted


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
    fresh_perceptions: int = 0   # things they saw or went through recently and have not had a chance to raise

    def text(self) -> str:
        if not (self.facts or self.memories or self.last_spoke):
            return ""
        parts = [f"What {self.name} knows and remembers (only {self.name} knows this; "
                 f"anything not listed here, {self.name} does not know):"]
        if self.last_spoke:
            parts.append(f"- {self.last_spoke}")
        parts += [f"- {line}" for line in self.facts]
        parts += [f"- {line}" for line in self.memories]
        if self.fresh_perceptions:
            parts.append(f"- {self.name} may bring up something they recently saw or went through without being asked, "
                         f"if it fits the moment. {self.name} only knows what they perceived, not why it happened.")
        return "\n".join(parts)


def recall_for(store: WorldStore, npc_id: int, now_gt: float, player_id: int | None = None,
               mark_rehearsed: bool = True) -> Recall:
    name = store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,))
    recall = Recall(npc_id, name)

    facts = store.query(
        "SELECT know_id, prompt_line FROM v_known_facts WHERE holder_id = ? ORDER BY learned_gt DESC, know_id DESC LIMIT ?",
        (npc_id, MAX_FACTS))
    for know_id, line in reversed(facts):
        recall.know_ids.append(know_id)
        recall.facts.append(line)

    memories = store.query(
        "SELECT mem_id, content, occurred_gt, source_kind, interpretation, rehearsal_count, importance"
        " FROM v_visible_memory WHERE owner_id = ?"
        " ORDER BY occurred_gt DESC, importance DESC LIMIT ?", (npc_id, MAX_MEMORIES))
    for mem_id, content, occurred, source_kind, interpretation, rehearsals, importance in reversed(memories):
        recall.mem_ids.append(mem_id)
        how = {"experienced": ", it happened to me", "witnessed": ", I saw it myself"}.get(source_kind or "", "")
        line = f"({time_ago(now_gt - occurred)}{how}) {content}"
        if interpretation:
            line += f" What I made of it: {interpretation}"
        recall.memories.append(line)
        if source_kind in ("experienced", "witnessed") and rehearsals == 0 and importance >= 0.4 \
                and now_gt - occurred <= FRESH_DAYS:
            recall.fresh_perceptions += 1

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

    if mark_rehearsed and recall.mem_ids:
        with store.transaction() as db:
            db.executemany("UPDATE memory SET rehearsal_count = rehearsal_count + 1, last_recalled_gt = ? WHERE mem_id = ?",
                           [(now_gt, m) for m in recall.mem_ids])
    return recall
