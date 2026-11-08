"""Recall: the lines of one NPC's own knowledge and memories that go into that NPC's prompt.

Reads only through the timeline-aware views (``v_known_facts``, ``v_visible_memory``) and only
rows the NPC holds or owns, so nothing another NPC knows can reach this prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.store.world_store import WorldStore

MAX_FACTS = 40
MAX_MEMORIES = 8
MAX_HEARD = 3
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
    opinion: str | None = None                       # how they feel about the player right now
    heard: list[str] = field(default_factory=list)  # news other people told them
    romance: list[str] = field(default_factory=list)  # their own love life (social/romance.py)
    fresh_perceptions: int = 0   # things they saw or went through recently and have not had a chance to raise

    def text(self) -> str:
        if not (self.facts or self.memories or self.last_spoke or self.opinion or self.heard or self.romance):
            return ""
        parts = [f"What {self.name} knows and remembers (only {self.name} knows this; "
                 f"anything not listed here, {self.name} does not know):"]
        if self.last_spoke:
            parts.append(f"- {self.last_spoke}")
        parts += [f"- {line}" for line in self.facts]
        parts += [f"- {line}" for line in self.memories]
        parts += [f"- {line}" for line in self.heard]
        parts += [f"- {line}" for line in self.romance]
        if self.opinion:
            parts.append(f"- {self.opinion}")
        if self.fresh_perceptions:
            parts.append(f"- {self.name} may bring up something they recently saw or went through without being asked, "
                         f"if it fits the moment. {self.name} only knows what they perceived, not why it happened.")
        return "\n".join(parts)


def recall_for(store: WorldStore, npc_id: int, now_gt: float, player_id: int | None = None,
               mark_rehearsed: bool = True) -> Recall:
    name = store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,))
    recall = Recall(npc_id, name)

    facts = store.query(
        "SELECT know_id, prompt_line FROM v_known_facts WHERE holder_id = ? AND predicate NOT LIKE 'claim:%' ORDER BY learned_gt DESC, know_id DESC LIMIT ?",
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

    _heard(store, recall, now_gt)
    from livingworld.social import romance
    if romance.romance_enabled():
        with store.reading() as db:
            recall.romance = romance.recall_lines(db, npc_id, now_gt)
    if player_id is not None:
        recall.opinion = opinion_line(store, npc_id, player_id)

    if mark_rehearsed and recall.mem_ids:
        with store.transaction() as db:
            db.executemany("UPDATE memory SET rehearsal_count = rehearsal_count + 1, last_recalled_gt = ? WHERE mem_id = ?",
                           [(now_gt, m) for m in recall.mem_ids])
    return recall


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
