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

Gestures (BL2, read in VR through VRIK; ``target`` is the NPC the gesture was made to, ``about`` is who
was pointed at, as a name): ``wave``, ``nod``, ``shake`` (head), ``point``, ``beckon``, ``hands_up``,
``thumbs_up``, ``thumbs_down``, ``open_arms`` (both hands held out wide at chest height, offering a hug;
``agreed=1`` when the NPC had already agreed to one, see ``livingworld/touch``).

Later gestures, each group behind its own switch (``bodylang/__init__.py``):

- ``handshake``: the player held a hand out toward the NPC, open, at about waist height, to shake.
  ``back=1`` when the NPC held theirs out in return (the game decides at once: not someone who dislikes
  the player, is wary of them, is fighting, sitting or busy); for a few seconds after, the player's hand
  on theirs is an agreed handshake (``consent=handshake`` on the grab record), not a grab.
- ``salute``: a hand flat at the brow (``style=brow``) or a fist over the heart (``style=chest``).
  ``back=1`` when the NPC (a guard) returned it.
- Adult only, off unless switched on, adults only (children are never sent anything): ``stroke``, the
  player's hand making a repeated stroking motion at their own groin (``n`` = how many sets of five strokes
  in a row), and ``point_down``, the player pointing down at their own groin, which the NPC's eyes follow
  for a moment. ``naked=1`` when the player had nothing on there. These only tell the NPC what happened;
  what they make of it is theirs alone (their personality, boundaries and how they feel about the player),
  and nothing is ever started or agreed because of them.

Apart from ``peek``, only the player's behaviour is ever a percept (see the loop guards in ``__init__``).
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.bodylang.state import Feelings
from livingworld.perception.records import RECORD_VERSION, BadRecord, GameActor, _actor

KIND = "presence"
GESTURES = ("wave", "nod", "shake", "point", "beckon", "hands_up", "thumbs_up", "thumbs_down", "open_arms")
HANDSHAKE = ("handshake",)
SALUTE = ("salute",)
ADULT = ("stroke", "point_down")
WHATS = ("approach", "crowd", "backed_off", "weapon", "stare", "peek") + GESTURES + HANDSHAKE + SALUTE + ADULT
OBSERVATION_MINUTES = 15.0   # older observations are not worth telling the dialogue model
ANSWERS = ("nod", "shake")   # a silent yes or no only means something right after a question
ANSWER_MINUTES = 2.0
MAX_OBSERVATIONS = 3
FELT_LINE_AT = 0.3           # a feeling this strong gets a line in the prompt


def is_presence(raw: str) -> bool:
    return "|type=presence" in (raw or "").lower()


def _what_of(raw: str) -> str:
    low = (raw or "").lower()
    at = low.find("|what=")
    return low[at + 6:].split("|", 1)[0].strip() if at >= 0 else ""


def is_gesture_record(raw: str) -> bool:
    """One of the BL2 gestures (``MANTELLA_LW_GESTURES``)."""
    return _what_of(raw) in GESTURES


def gesture_group(raw: str) -> str | None:
    """Which switch a record falls under: 'gestures', 'handshake', 'salute', 'adult', or None (not a gesture)."""
    what = _what_of(raw)
    if what in GESTURES:
        return "gestures"
    if what in HANDSHAKE:
        return "handshake"
    if what in SALUTE:
        return "salute"
    if what in ADULT:
        return "adult"
    return None


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
    about: str | None = None   # who the player pointed at (a name)
    agreed: bool = False       # open_arms: the NPC had already agreed to a hug
    back: bool = False         # handshake / salute: the NPC answered in kind
    style: str | None = None   # salute: 'brow' or 'chest'
    naked: bool = False        # stroke / point_down: nothing on the player's groin

    @property
    def is_gesture(self) -> bool:
        return self.what in GESTURES + HANDSHAKE + SALUTE + ADULT

    @property
    def is_adult(self) -> bool:
        return self.what in ADULT


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
                    duration_s=_float(fields.get("dur")), count=count, about=_about(fields.get("about")),
                    agreed=fields.get("agreed") == "1", back=fields.get("back") == "1",
                    style=_style(fields.get("style")), naked=fields.get("naked") == "1")


def _style(value: str | None) -> str | None:
    word = (value or "").strip().lower()
    return word if word in ("brow", "chest") else None


def _about(value: str | None) -> str | None:
    text = " ".join((value or "").split())[:60]
    return text or None


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
    if p.what == "hands_up":   # a surrender takes the edge off fear and anger
        return {"fear": -0.15, "irritation": -0.08}
    if p.what == "wave" and not bond.hostile:
        return {"unease": -0.03}
    if p.what == "thumbs_up":
        return {"unease": -0.03, "irritation": -0.02}
    if p.what == "thumbs_down":
        return {"irritation": round(0.05 * (0.5 + t("temper")), 3)}
    if p.what == "beckon" and bond.hostile:
        return {"unease": round(0.05 * (0.5 + t("anxiety")), 3)}
    if p.what == "handshake":
        return {"unease": -0.04, "irritation": -0.02} if p.back else {}
    if p.what == "salute":
        return {"unease": -0.02} if p.back else {}
    if p.what in ADULT:
        return _adult_appraisal(p, t, bond)
    return {}   # nod, shake, point: their meaning is for the conversation, not a feeling


def _adult_appraisal(p: Presence, t, bond: Bond) -> dict[str, float]:
    """A lewd gesture made at someone. Modest, reserved people are the most put out; someone close to the
    player much less; a hostile one is annoyed. Never a reason to want anything (see ``observation``)."""
    strength = 1.0 if p.what == "stroke" else 0.55
    if p.what == "stroke":
        strength *= 1.0 + 0.4 * min(max(p.count - 1, 0), 3)   # kept on and on
    if p.naked:
        strength *= 1.3
    shy = 0.4 + 0.6 * t("modesty") + 0.3 * (t("reserve") - 0.5)
    if bond.hostile:
        return {"irritation": round(0.12 * strength * (0.5 + t("temper")), 3),
                "unease": round(0.08 * strength * (0.5 + t("anxiety")), 3)}
    soften = 0.4 if bond.close else 1.0
    out = {"embarrassment": round(0.14 * strength * shy * soften, 3)}
    if not bond.close:
        out["unease"] = round(0.08 * strength * (0.5 + t("anxiety")) * shy, 3)
        if t("modesty") >= 0.7:
            out["irritation"] = round(0.06 * strength * (0.5 + t("temper")), 3)
    return out


ADULT_TAIL = ("How {npc} takes it is entirely {npc}'s own, true to who {npc} is, {npc}'s boundaries and how {npc} "
              "feels about {player}: amused, flustered, offended, disgusted, unimpressed or interested are all "
              "possible. It obliges {npc} to nothing, and it is not an invitation {npc} has accepted.")


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
    if p.what == "wave":
        return f"{player_name} waved at them."
    if p.what == "nod":
        return f"{player_name} nodded (a silent yes)."
    if p.what == "shake":
        return f"{player_name} shook their head (a silent no)."
    if p.what == "point":
        if p.about and p.about != p.target.name:
            return f"{player_name} pointed at {p.about}."
        if p.about:
            return f"{player_name} pointed at them."
        return f"{player_name} pointed off into the distance."
    if p.what == "beckon":
        return f"{player_name} beckoned them over, a \"come here\" gesture."
    if p.what == "hands_up":
        return f"{player_name} raised both hands above their head, as if surrendering."
    if p.what == "thumbs_up":
        return f"{player_name} gave them a thumbs up."
    if p.what == "thumbs_down":
        return f"{player_name} gave them a thumbs down."
    if p.what == "open_arms":
        if p.agreed:
            return f"{player_name} opened their arms for the hug they had agreed to, and they opened theirs back."
        return (f"{player_name} opened their arms wide toward them, offering a hug. Whether to accept is entirely "
                f"their own choice.")
    npc = p.target.name
    if p.what == "handshake":
        if p.back:
            return f"{player_name} held out a hand to shake, and {npc} offered theirs in return."
        return f"{player_name} held out a hand to shake; {npc} did not take it."
    if p.what == "salute":
        how = {"brow": "a hand to the brow", "chest": "a fist over the heart"}.get(p.style or "", "")
        text = f"{player_name} saluted {npc}{f' ({how})' if how else ''}."
        return text + (f" {npc} returned the salute." if p.back else "")
    if p.what in ADULT:
        where = "his bare manhood" if p.naked else "his groin"
        if p.what == "stroke":
            times = " again and again" if p.count > 1 else ""
            text = (f"Right in front of {npc}, {player_name} made a repeated stroking motion with his hand at {where}"
                    f"{times}, plainly miming pleasuring himself.")
        else:
            text = f"{player_name} pointed down at {where}, and {npc}'s eyes followed his finger there for a moment."
        return text + " " + ADULT_TAIL.format(npc=npc, player=player_name)
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
