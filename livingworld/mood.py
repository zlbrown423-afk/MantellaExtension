"""The mood meter: how each NPC in a conversation with the player feels right now.

The NPC's own reply says it. Their prompt asks them to open every reply with a short tag such as
``<mood wary 4>``: one feeling word and a number from 0 (furious, hostile, miserable) through 5 (calm)
to 10 (delighted, warm). The tag is cut out of the reply as it streams in, before Mantella splits the
reply into sentences, so it is never spoken, shown as a subtitle or kept in the conversation history.
No extra AI call is made.

Each new mood is stored in ``emotion_state`` and sent to the game with the NPC's next line, which shows
it as a notification such as ``Ysolda: amused [=======---] tipsy``. The next conversation with the same
NPC within half a game day starts from the mood they were left in.

Mood is about the moment. It never changes a relationship, and it never changes what an NPC is willing
to do with their body: the same rule as drink (see ``perception/intoxication.py``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

NEUTRAL = 5
CARRY_OVER_DAYS = 0.5     # a mood from a conversation this recent (game days) carries into the next one
MAX_TAG_LENGTH = 48       # a "<" not closed within this many characters is not a mood tag
METER_WIDTH = 10

# <mood wary 4>, <mood: wary, 4/10>, <Mood=wary 4> ...
TAG = re.compile(r"<\s*mood\s*[:=]?\s*([A-Za-z][A-Za-z' -]{0,28}?)?\s*[,;/-]?\s*(\d{1,2})?\s*(?:/\s*10)?\s*>",
                 re.IGNORECASE)


@dataclass(frozen=True)
class Mood:
    label: str
    score: int   # 0..10, 5 is calm

    @staticmethod
    def make(label: str | None, score: int | None) -> "Mood | None":
        label = " ".join((label or "").split()).lower()
        if not label and score is None:
            return None
        score = NEUTRAL if score is None else max(0, min(10, score))
        return Mood(label or describe(score), score)

    def meter(self) -> str:
        return "[" + "=" * self.score + "-" * (METER_WIDTH - self.score) + "]"


def describe(score: int) -> str:
    if score <= 1:
        return "furious"
    if score <= 3:
        return "upset"
    if score <= 4:
        return "uneasy"
    if score <= 6:
        return "calm"
    if score <= 8:
        return "pleased"
    return "delighted"


def parse(tag_text: str) -> Mood | None:
    match = TAG.fullmatch(tag_text.strip())
    if not match:
        return None
    return Mood.make(match.group(1), int(match.group(2)) if match.group(2) else None)


def meter_text(name: str, mood: Mood, drink_stage: str | None = None) -> str:
    """The notification the game shows, e.g. ``Ysolda: amused [=======---] tipsy``."""
    text = f"{name}: {mood.label} {mood.meter()}"
    return f"{text} {drink_stage}" if drink_stage else text


class MoodFilter:
    """Cuts mood tags out of a streamed reply and reports each one.

    ``feed`` takes each streamed chunk and returns the text with any tag removed. Text from a ``<`` is
    held back until it can be told apart from a tag (closed by ``>`` or longer than a tag can be), so a
    tag split across chunks is still caught. A reply that ends while text is held back loses only that
    unfinished fragment; ``flush`` returns it if the caller wants it.
    """

    def __init__(self, on_mood: Callable[[Mood], None]) -> None:
        self.on_mood = on_mood
        self._held = ""
        self._emitted = False   # any text passed on yet

    def feed(self, chunk: str) -> str:
        text = self._held + (chunk or "")
        self._held = ""
        out = []
        while text:
            start = text.find("<")
            if start < 0:
                out.append(text)
                break
            out.append(text[:start])
            rest = text[start:]
            end = rest.find(">")
            if end < 0:
                if len(rest) <= MAX_TAG_LENGTH:
                    self._held = rest      # might be a tag still arriving
                    break
                out.append(rest[0])        # too long to be a tag
                text = rest[1:]
                continue
            candidate = rest[:end + 1]
            if TAG.fullmatch(candidate):
                mood = parse(candidate)
                if mood:
                    self.on_mood(mood)
                text = rest[end + 1:]
            else:
                out.append(rest[0])
                text = rest[1:]
        result = "".join(out)
        if not self._emitted:
            # No stray space at the start of the reply, and no colon: the model sometimes writes
            # "<mood wary 6>: What is it?", then copies ": ..." from its own history into later replies.
            result = result.lstrip(" \t\r\n:;")
        self._emitted = self._emitted or bool(result.strip())
        return result

    def flush(self) -> str:
        held, self._held = self._held, ""
        return "" if TAG.match(held + ">") else held


# ---- prompt ----
INSTRUCTION = (
    "Mood: open every reply as {name} with a mood tag such as <mood wary 4>: one word for how {name} feels "
    "right now, then a number from 0 (furious, hostile or miserable) through 5 (calm) to 10 (delighted and warm). "
    "The tag is never spoken; it only feeds a mood meter the player can see. Let it move with what happens: "
    "kindness, gifts, flattery, insults, threats, bad news, drink. A mood never changes what {name} is "
    "willing to do with their body.")
CARRY_OVER = "{name} comes into this conversation still feeling {label} ({score}/10) from a little while ago."


def prompt_lines(name: str, carried: Mood | None) -> list[str]:
    lines = [INSTRUCTION.format(name=name)]
    if carried:
        lines.append(CARRY_OVER.format(name=name, label=carried.label, score=carried.score))
    return lines


# ---- storage (emotion_state) ----
def save(db, npc_id: int, mood: Mood, game_time: float) -> None:
    """Store the NPC's mood, keeping anything else in ``mood_json`` (body language keeps its feelings there)."""
    data: dict = {}
    row = db.execute("SELECT mood_json FROM emotion_state WHERE npc_id = ?", (npc_id,)).fetchone()
    if row:
        try:
            loaded = json.loads(row[0] or "{}")
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            data = {}
    data.update({"label": mood.label, "score": mood.score})
    payload = json.dumps(data)
    db.execute("INSERT INTO emotion_state (npc_id, mood_json, baseline_json, updated_gt) VALUES (?, ?, '{}', ?)"
               " ON CONFLICT(npc_id) DO UPDATE SET mood_json = excluded.mood_json, updated_gt = excluded.updated_gt",
               (npc_id, payload, game_time))


def carried_over(db, npc_id: int, now_gt: float) -> Mood | None:
    """The NPC's stored mood if it was set within ``CARRY_OVER_DAYS`` before ``now_gt``.

    A mood stored after ``now_gt`` belongs to a later point on a timeline an older save left behind,
    so it is ignored, like the rest of that timeline."""
    row = db.execute("SELECT mood_json, updated_gt FROM emotion_state WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row:
        return None
    mood_json, updated_gt = row[0], row[1]
    if updated_gt > now_gt + 1e-9 or now_gt - updated_gt > CARRY_OVER_DAYS:
        return None
    try:
        data = json.loads(mood_json)
        return Mood.make(data.get("label"), int(data["score"]))
    except (ValueError, KeyError, TypeError):
        return None
