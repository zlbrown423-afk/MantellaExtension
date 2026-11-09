"""Short-lived feelings that body language reads and writes, kept beside the mood meter.

They live in ``emotion_state.mood_json`` under the key ``"body"``, next to the mood meter's ``label`` and
``score`` (``mood.save`` keeps them when it writes a new mood). Each feeling is 0..1 and fades with a
half-life in game minutes; ``crowded`` counts recent times the player stood too close and fades too.

A row stamped after ``now`` belongs to a later point on a timeline an older save left behind, so it is
ignored, like the rest of that timeline (the same rule as ``mood.carried_over``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

FEELINGS = ("irritation", "unease", "fear", "embarrassment")
HALF_LIFE_MIN = {"irritation": 20.0, "unease": 15.0, "fear": 15.0, "embarrassment": 10.0, "crowded": 30.0}
MIN_PER_DAY = 1440.0


@dataclass
class Feelings:
    values: dict[str, float] = field(default_factory=lambda: {f: 0.0 for f in FEELINGS})
    crowded: float = 0.0     # recent times the player stood within the comfort distance (fading count)

    def get(self, name: str) -> float:
        return self.values.get(name, 0.0)

    def add(self, deltas: dict[str, float]) -> None:
        for name, delta in deltas.items():
            if name == "crowded":
                self.crowded = round(max(0.0, self.crowded + delta), 3)
            elif name in FEELINGS:
                self.values[name] = round(max(0.0, min(1.0, self.get(name) + delta)), 3)

    def strongest(self) -> tuple[str, float]:
        name = max(FEELINGS, key=self.get)
        return name, self.get(name)

    def to_json(self) -> dict:
        return {**{f: self.get(f) for f in FEELINGS}, "crowded": self.crowded}


def decayed(value: float, minutes: float, half_life: float) -> float:
    if minutes <= 0:
        return value
    return value * 0.5 ** (minutes / half_life)


def load(db, npc_id: int, now_gt: float) -> Feelings:
    row = db.execute("SELECT mood_json, updated_gt FROM emotion_state WHERE npc_id = ?", (npc_id,)).fetchone()
    feelings = Feelings()
    if not row:
        return feelings
    try:
        body = (json.loads(row[0] or "{}") or {}).get("body") or {}
        stamped = float(body.get("gt", row[1]))
    except (ValueError, TypeError, AttributeError):
        return feelings
    if stamped > now_gt + 5.0 / MIN_PER_DAY:   # from a timeline an older save left behind (a few minutes of
        return feelings                         # slack: records and the context's game time are sent apart)
    minutes = max(0.0, (now_gt - stamped) * MIN_PER_DAY)
    for name in FEELINGS:
        try:
            feelings.values[name] = round(decayed(float(body.get(name, 0.0)), minutes, HALF_LIFE_MIN[name]), 3)
        except (TypeError, ValueError):
            pass
    try:
        feelings.crowded = round(decayed(float(body.get("crowded", 0.0)), minutes, HALF_LIFE_MIN["crowded"]), 3)
    except (TypeError, ValueError):
        pass
    return feelings


def save(db, npc_id: int, feelings: Feelings, now_gt: float) -> None:
    """Store ``feelings`` as of ``now_gt``, keeping the mood meter's own keys."""
    row = db.execute("SELECT mood_json FROM emotion_state WHERE npc_id = ?", (npc_id,)).fetchone()
    try:
        data = json.loads(row[0] or "{}") if row else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    data["body"] = {**feelings.to_json(), "gt": now_gt}
    if row:
        db.execute("UPDATE emotion_state SET mood_json = ? WHERE npc_id = ?", (json.dumps(data), npc_id))
    else:
        db.execute("INSERT INTO emotion_state (npc_id, mood_json, baseline_json, updated_gt) VALUES (?, ?, '{}', ?)",
                   (npc_id, json.dumps(data), now_gt))
