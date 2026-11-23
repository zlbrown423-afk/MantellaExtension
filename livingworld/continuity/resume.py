"""Picking a conversation back up after a save load.

What happens without this: the save holds Mantella's conversation quest still running, so the game's
``OnPlayerLoadGame`` ends it (an end request) and the player has to start talking again. The NPC only saw
the generic "last time we talked" recap, and the end request carried the *loaded save's* game time, which
stamped the abandoned conversation (its memories, its Mantella summary) as if it ended before the save.
Those rows then showed up in the loaded game, although they never happened there.

What this adds (all on unless ``MANTELLA_LW_RESUME=0``):

1. **The end request sent at a load does not move time backwards.** ``end_time`` keeps the conversation's own
   latest game time, so what was said after the save stays on the timeline the player left (see
   ``store/timeline.py``) and is forgotten in the loaded game, like everything else after the save.
2. **The conversation id travels in the save.** The start reply carries ``mantella_lw_conv_id``; the game keeps
   it in the save and, when it starts the conversation again after a load, sends it back as
   ``mantella_lw_resume_conv``.
3. **The NPC knows the talk was interrupted, not over.** The lines of that conversation the loaded save may
   see (said at or before the save) are shown in the prompt as the conversation in progress, so the NPC
   carries on instead of greeting the player again.
4. **No memory is extracted twice.** When the old conversation's own memories cannot be seen from the loaded
   save (it ended after the save), its visible lines are copied into the new conversation, so the NPC still
   remembers them afterwards; extraction then runs once, on the new timeline. When its memories can be seen
   (nothing was said after the save), nothing is copied and the lines are only shown.

Nothing is changed or deleted in the old conversation. Every resume is written to ``conversation_resume``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

ENV = "MANTELLA_LW_RESUME"
_OFF = {"0", "false", "no", "off"}

KEY_CONV_ID = "mantella_lw_conv_id"          # start reply: this conversation's id, kept in the save
KEY_RESUME_CONV = "mantella_lw_resume_conv"  # start request: the conversation that was running in the loaded save
KEY_RESUME_OFF = "mantella_lw_resume_off"    # start reply: the game should not restart conversations after a load
KEY_LOAD_END = "mantella_lw_load_end"        # end request: sent by the game's OnPlayerLoadGame, not by the player

EPSILON_DAYS = 0.0007        # about one game minute, as in store/timeline.py
MAX_GAP_DAYS = 0.25          # the last visible line must be at most six game hours before now
MAX_LINES = 12               # lines shown in the prompt
MAX_CHARS = 240              # per line


def resume_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def end_time(end_timestamp: float | None, conversation_days: float | None, load_end: bool) -> tuple[float | None, bool]:
    """The game time to close a conversation at, and whether the end came from a save load.

    A running conversation's clock never goes backwards, so an end time earlier than the conversation's own
    latest game time can only come from a save load (the game sends the loaded save's time). That end keeps
    the conversation's own time instead. ``load_end`` is the game's own flag for the same case."""
    if end_timestamp is None or conversation_days is None:
        return end_timestamp, bool(load_end)
    went_back = float(end_timestamp) < float(conversation_days) - EPSILON_DAYS
    if load_end or went_back:
        return max(float(end_timestamp), float(conversation_days)), True
    return end_timestamp, False


@dataclass
class Line:
    seq: int
    speaker_id: int
    speaker: str
    text: str
    voice_model: str | None
    game_time: float


@dataclass
class ResumePlan:
    old_conv_id: int
    lines: list[Line]
    copy: bool                                   # copy the lines into the new conversation
    heard_by: set[int] = field(default_factory=set)   # who was there for them (participants of the old one)
    location: str = ""
    last_gt: float = 0.0
    skipped: str = ""                            # why there is no plan (for the log)


def visible_lines(db, conv_id: int) -> list[Line]:
    """The lines of ``conv_id`` the active timeline may see: said at or before its cutoff."""
    rows = db.execute(
        "SELECT u.seq, u.speaker_id, n.display_name, u.text, u.voice_model, u.game_time FROM v_utterance u"
        " JOIN npc n ON n.npc_id = u.speaker_id"
        " JOIN v_timeline_scope s ON s.timeline_id = u.timeline_id AND u.game_time <= s.cutoff_gt"
        " WHERE u.conv_id = ? ORDER BY u.seq", (conv_id,)).fetchall()
    return [Line(int(r[0]), int(r[1]), str(r[2]), str(r[3] or ""), r[4], float(r[5])) for r in rows]


def closed_visibly(db, conv_id: int) -> bool:
    """True when the conversation's end (and so the memories extraction stamps with it) is visible from the
    active timeline. An unclosed conversation counts by its start, as extraction would stamp it."""
    return db.execute(
        "SELECT 1 FROM conversation c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
        " AND COALESCE(c.ended_gt, c.started_gt) <= s.cutoff_gt WHERE c.conv_id = ?", (conv_id,)).fetchone() is not None


def plan(db, old_conv_id: int, new_conv_id: int, present: set[int], player_id: int | None, now: float) -> ResumePlan:
    """Whether and how the conversation ``old_conv_id`` (running in the loaded save) can be picked up by the new
    one. Refuses anything that does not clearly belong: another world's id, a conversation the loaded save
    never reached, one without the player or without anyone here now, or one long past."""
    def no(why: str) -> ResumePlan:
        return ResumePlan(old_conv_id, [], False, skipped=why)

    if old_conv_id == new_conv_id:
        return no("same conversation")
    row = db.execute(
        "SELECT c.kind, c.location FROM conversation c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
        " AND c.started_gt <= s.cutoff_gt WHERE c.conv_id = ?", (old_conv_id,)).fetchone()
    if row is None:
        return no("not a conversation this save reached")
    if row[0] != "player":
        return no("not a conversation with the player")
    people = {int(r[0]): r[1] for r in db.execute(
        "SELECT npc_id, left_seq FROM participant WHERE conv_id = ?", (old_conv_id,)).fetchall()}
    if player_id is None or player_id not in people:
        return no("the player was not in it")
    npcs_here = {i for i in present if i != player_id}
    if not npcs_here & set(people):
        return no("nobody from it is here")
    lines = visible_lines(db, old_conv_id)
    if not lines:
        return no("nothing was said before the save")
    last_gt = lines[-1].game_time
    if now - last_gt > MAX_GAP_DAYS or last_gt > now + EPSILON_DAYS:
        return no(f"its last line is not from just before this save (day {last_gt:.3f}, now {now:.3f})")
    last_seq = lines[-1].seq
    heard_by = {i for i, left in people.items() if left is None or left >= last_seq}
    return ResumePlan(old_conv_id, lines, not closed_visibly(db, old_conv_id), heard_by, str(row[1] or ""), last_gt)


def apply(db, new_conv_id: int, resume: ResumePlan) -> int:
    """Write the resume: the link row, and the copied lines when ``resume.copy``. Returns lines copied.
    The new conversation must not hold lines yet. People here now who were not there for the copied lines
    join after them, so extraction never gives them memories of what they did not hear."""
    copied = 0
    if resume.copy:
        timeline = db.execute("SELECT timeline_id FROM conversation WHERE conv_id = ?", (new_conv_id,)).fetchone()[0]
        if db.execute("SELECT COUNT(*) FROM utterance WHERE conv_id = ?", (new_conv_id,)).fetchone()[0]:
            resume.copy = False
        else:
            for seq, line in enumerate(resume.lines, start=1):
                db.execute("INSERT INTO utterance (timeline_id, conv_id, seq, speaker_id, text, voice_model, game_time)"
                           " VALUES (?,?,?,?,?,?,?)",
                           (timeline, new_conv_id, seq, line.speaker_id, line.text, line.voice_model, line.game_time))
                copied += 1
            ids = sorted(resume.heard_by) or [-1]
            db.execute(f"UPDATE participant SET joined_seq = ? WHERE conv_id = ? AND joined_seq < ?"
                       f" AND npc_id NOT IN ({','.join('?' * len(ids))})", (copied, new_conv_id, copied, *ids))
    db.execute("INSERT OR REPLACE INTO conversation_resume (conv_id, resumed_conv_id, lines_copied) VALUES (?,?,?)",
               (new_conv_id, resume.old_conv_id, copied))
    return copied


def resumed_from(db, conv_id: int | None) -> int | None:
    """The conversation ``conv_id`` picked back up, if it is a resume."""
    if conv_id is None:
        return None
    row = db.execute("SELECT resumed_conv_id FROM conversation_resume WHERE conv_id = ?", (conv_id,)).fetchone()
    return int(row[0]) if row else None
