"""What the player's body language does to an NPC, and how that NPC shows it. Rules only, no LLM.

The game sends one ``type=presence`` record per meaningful moment, never per tick::

    v=1|key=<nonce>-<n>|type=presence|gt=<game days + 1>|loc=<place>|actor=<player>|target=<npc>
      |what=crowd|dist=0.62|dur=4.5|n=2

``what`` is one of:

- ``approach``: the player came from more than 5 m to within 3 m in a few seconds
- ``crowd``: the player stayed inside the NPC's comfort distance (``dist`` = closest metres, ``dur`` =
  seconds inside, ``n`` = how many times in the last few minutes, counted by the game)
- ``backed_off``: the player left the comfort distance again after crowding
- ``weapon``: the player drew a weapon or readied a spell within 6 m of the NPC
- ``stare``: the player kept facing the NPC from within 2 m for several seconds (head direction only:
  there is no eye tracking, so this is worded as "facing", never as "eyes")
- ``peek``: the naked player is within a few metres and the NPC (a woman) glanced down at him (``peek.py``).
  This one is the NPC's own glance, sent only so her next line may know it; it changes no feelings.

Apart from ``peek``, only the player's behaviour is ever a percept (see the loop guards in ``__init__``).
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.bodylang.state import Feelings
from livingworld.perception.records import RECORD_VERSION, BadRecord, GameActor, _actor

KIND = "presence"
WHATS = ("approach", "crowd", "backed_off", "weapon", "stare", "peek")
OBSERVATION_MINUTES = 15.0   # older observations are not worth telling the dialogue model
MAX_OBSERVATIONS = 3
FELT_LINE_AT = 0.3           # a feeling this strong gets a line in the prompt


def is_presence(raw: str) -> bool:
    return "|type=presence" in (raw or "").lower()


@dataclass(frozen=True)
class Presence:
    key: str
    game_time: float
    location: str | None
    actor: GameActor        # the player
    target: GameActor       # the NPC who noticed
    what: str
    distance_m: float | None = None
    duration_s: float | None = None
    count: int = 1


def _float(value: str | None) -> float | None:
    try:
        return round(float(value), 2) if value not in (None, "") else None
    except ValueError:
        return None


def parse(raw: str) -> Presence:
    fields: dict[str, str] = {}
    for part in raw.split("|"):
        if "=" in part:
            name, value = part.split("=", 1)
            fields[name.strip().lower()] = value
    if fields.get("v") != RECORD_VERSION:
        raise BadRecord(f"unknown record version {fields.get('v')!r}")
    for required in ("key", "gt", "actor", "target", "what"):
        if not fields.get(required):
            raise BadRecord(f"presence record has no {required}")
    what = fields["what"].strip().lower()   # Papyrus may recase string literals
    if what not in WHATS:
        raise BadRecord(f"unknown presence kind {what!r}")
    actor, target = _actor(fields["actor"]), _actor(fields["target"])
    if not actor.is_player or target.is_player:
        raise BadRecord("presence records are about the player and one NPC")   # loop guard: never NPC to NPC
    try:
        count = max(1, int(float(fields.get("n") or 1)))
    except ValueError:
        count = 1
    return Presence(key=fields["key"], game_time=float(fields["gt"]), location=fields.get("loc") or None,
                    actor=actor, target=target, what=what, distance_m=_float(fields.get("dist")),
                    duration_s=_float(fields.get("dur")), count=count)


@dataclass
class Bond:
    """How the NPC sees the player (v_relationship; neutral when there is no row)."""
    trust: float = 0.0
    affinity: float = 0.0
    familiarity: float = 0.0
    fear: float = 0.0

    @property
    def close(self) -> bool:
        return self.affinity >= 0.3 or self.trust >= 0.4 or self.familiarity >= 0.6

    @property
    def hostile(self) -> bool:
        return self.trust <= -0.3 or self.affinity <= -0.3 or self.fear >= 0.4


def bond_of(db, npc_id: int, player_id: int) -> Bond:
    row = db.execute("SELECT trust, affinity, familiarity, fear FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (npc_id, player_id)).fetchone()
    return Bond(*(float(v or 0.0) for v in row)) if row else Bond()


def _t(traits: dict[str, float], name: str) -> float:
    return traits.get(name, 0.5)


def appraise(p: Presence, traits: dict[str, float], bond: Bond) -> dict[str, float]:
    """Feeling changes for the NPC who noticed ``p``. Different people take the same thing differently."""
    t = lambda name: _t(traits, name)
    if p.what == "crowd":
        repeat = 1.0 + 0.5 * min(max(p.count - 1, 0), 3)
        base = 0.12 * (0.5 + t("personal_space")) * repeat * (0.4 if bond.close else 1.0)
        if (p.distance_m or 1.0) < 0.5:
            base *= 1.3
        return {"irritation": round(base * (0.5 + t("temper")), 3), "unease": round(base * (0.5 + t("anxiety")), 3),
                "crowded": 1.0}
    if p.what == "backed_off":
        return {"unease": -0.08, "irritation": -0.04}
    if p.what == "approach":
        if bond.hostile:
            return {"unease": round(0.08 * (0.5 + t("anxiety")), 3)}
        return {}
    if p.what == "weapon":
        fear = 0.25 * (1.2 - t("bold")) * (0.5 if bond.close else 1.0)
        return {"fear": round(max(0.02, fear), 3), "irritation": round(0.1 * t("temper"), 3)}
    if p.what == "stare":
        if bond.close and t("warmth") >= 0.6:
            return {}
        return {"embarrassment": round(0.1 * (0.5 + t("reserve")), 3),
                "unease": round(0.05 * (0.5 + t("anxiety")), 3)}
    return {}


def observation(p: Presence, player_name: str) -> str:
    """Neutral words for what happened. What it means is the NPC's to decide."""
    dist = f"{p.distance_m:.1f} m" if p.distance_m is not None else "close"
    if p.what == "crowd":
        text = f"{player_name} stood very close, {dist} away"
        if p.duration_s and p.duration_s >= 1.5:
            text += f", for about {p.duration_s:.0f} seconds"
        if p.count > 1:
            text += f" (that is {p.count} times in the last few minutes)"
        return text + "."
    if p.what == "backed_off":
        return f"{player_name} stepped back and gave some room again."
    if p.what == "approach":
        return f"{player_name} walked straight up from a distance."
    if p.what == "weapon":
        return f"{player_name} drew a weapon or readied a spell {dist} away."
    if p.what == "stare":
        return f"{player_name} kept facing them from {dist} away for several seconds."
    if p.what == "peek":
        return (f"{p.target.name}'s eyes went down to naked {player_name}'s manhood for a moment, {dist} away."
                f" Whether {player_name} noticed, and whether {p.target.name} owns up to it, is hers to play.")
    return ""


_WORDS = {"irritation": ("a little irritated", "irritated", "angry"),
          "unease": ("a little uneasy", "uneasy", "very uncomfortable"),
          "fear": ("wary", "frightened", "terrified"),
          "embarrassment": ("a little self-conscious", "embarrassed", "mortified")}


def felt_line(name: str, feelings: Feelings) -> str | None:
    parts = []
    for feeling in ("fear", "irritation", "unease", "embarrassment"):
        value = feelings.get(feeling)
        if value >= FELT_LINE_AT:
            words = _WORDS[feeling]
            parts.append(words[0] if value < 0.5 else words[1] if value < 0.75 else words[2])
    if not parts:
        return None
    return f"Because of this, {name} feels {' and '.join(parts[:2])} right now. Show it in how {name} speaks."


@dataclass(frozen=True)
class Profile:
    """How one NPC expresses themselves right now. The game acts on it without asking the server again."""
    name: str
    comfort_m: float     # personal space: the player inside this distance crowds them
    gaze: float          # 0 looks away, 0.5 glances, 1 holds the player's gaze
    guard: float         # 0 open, 1 arms folded and closed off
    steps_back: bool     # False: holds their ground when crowded
    peek: float = 0.0    # a woman glancing down at the naked player now and then (``peek.py``); 0 never

    def wire(self) -> str:
        return (f"{self.name}|{self.comfort_m:.2f}|{self.gaze:.2f}|{self.guard:.2f}|{1 if self.steps_back else 0}"
                f"|{self.peek:.2f}")


def _clamp(value: float, low: float, high: float) -> float:
    return round(max(low, min(high, value)), 2)


def profile(name: str, traits: dict[str, float], bond: Bond, feelings: Feelings, peek: float = 0.0) -> Profile:
    t = lambda trait: _t(traits, trait)
    f = feelings.get
    comfort = 0.9 * (0.6 + 0.8 * t("personal_space")) * (1.0 + 0.3 * (t("reserve") - 0.5))
    comfort += -0.3 * max(bond.affinity, 0.0) - 0.2 * bond.familiarity + 0.3 * bond.fear
    comfort += 0.5 * f("fear") + 0.3 * f("irritation") + 0.2 * f("unease") + 0.12 * min(feelings.crowded, 3.0)
    gaze = 0.5 + 0.3 * (t("warmth") - 0.5) + 0.3 * (t("dominance") - 0.5) - 0.3 * (t("anxiety") - 0.5)
    gaze += 0.2 * max(bond.affinity, 0.0) - 0.4 * f("embarrassment") - 0.3 * f("fear") - 0.15 * f("unease")
    guard = 0.2 + 0.6 * f("irritation") + 0.5 * f("fear") + 0.3 * f("unease") + 0.2 * (t("reserve") - 0.5)
    guard -= 0.2 * max(bond.affinity, 0.0)
    holds_ground = t("dominance") >= 0.7 and t("bold") >= 0.6 and f("fear") < 0.3
    return Profile(name, _clamp(comfort, 0.5, 2.0), _clamp(gaze, 0.0, 1.0), _clamp(guard, 0.0, 1.0), not holds_ground,
                   _clamp(peek, 0.0, 1.0))


def clamp_mood_score(new_score: int, previous: int | None, feelings: Feelings) -> int:
    """The mood tag the model writes is advice, not the truth: it moves at most 2 points from the last
    mood, and fresh fear or irritation caps it (a frightened NPC is not 'delighted')."""
    score = new_score
    if previous is not None:
        score = max(previous - 2, min(previous + 2, score))
    worst = max(feelings.get("fear"), feelings.get("irritation"))
    if worst >= 0.5:
        score = min(score, 5 - round(5 * (worst - 0.5)))
    return max(0, min(10, score))
