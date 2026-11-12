"""The adapter interface and preference matching, independent of any one framework."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from livingworld.personality.intimacy import GROUPS, Prefs

NONE = "none"          # an action that is not intimate (dancing, reading, holding an arm...)
UNKNOWN = "unknown"    # an action the adapter does not know: never assumed to be fine


@dataclass(frozen=True)
class SceneAction:
    action: str            # the framework's own action id
    actor: int | None      # index of the actor doing it
    target: int | None     # index of the actor it is done to


@dataclass(frozen=True)
class SceneInfo:
    scene_id: str          # the framework's own scene id, exactly as installed
    actors: tuple[str, ...]            # intended sex per position ('male' / 'female' / '')
    actions: tuple[SceneAction, ...]
    tags: tuple[str, ...] = ()
    furniture: str = ""


class Framework(Protocol):
    name: str

    def available(self) -> bool: ...
    def group_of(self, action: str) -> str: ...
    def scenes(self) -> dict[str, SceneInfo]: ...


@dataclass
class Verdict:
    scene_id: str
    ok: bool
    score: float                         # mean interest of her groups in it (0..100), boundaries make it not ok
    position: int | None
    groups: dict[str, str] = field(default_factory=dict)   # group -> 'giving' / 'receiving' / 'both'
    reasons: list[str] = field(default_factory=list)


def groups_in(framework: Framework, actions: list[str]) -> set[str]:
    """Activity groups for a list of the framework's action ids (unknown ids come back as UNKNOWN)."""
    out = set()
    for a in actions:
        g = framework.group_of(a)
        if g != NONE:
            out.add(g)
    return out


def judge(framework: Framework, prefs: Prefs, scene: SceneInfo, her_sex: str = "female",
          position: int | None = None) -> Verdict:
    """Is this installed scene compatible with her preferences? Never reads anything the framework did not list."""
    if position is None:
        position = next((i for i, s in enumerate(scene.actors) if s == her_sex), None)
    v = Verdict(scene.scene_id, True, 50.0, position)
    if position is None:
        v.ok, v.reasons = False, [f"no position for a {her_sex} actor"]
        return v
    if not prefs.enabled:
        v.ok, v.reasons = False, ["her intimacy preferences are switched off"]
        return v
    roles: dict[str, set[str]] = {}
    for act in scene.actions:
        if act.target is None:      # OStim: no target means the actor does it to themself
            act = SceneAction(act.action, act.actor, act.actor)
        if position not in (act.actor, act.target):
            continue
        group = framework.group_of(act.action)
        if group == NONE:
            continue
        role = "giving" if act.actor == position else "receiving"
        if act.actor == position and act.target == position:
            role = "both"
        roles.setdefault(group, set()).add(role)
    scores = []
    for group, rs in roles.items():
        v.groups[group] = "both" if len(rs) > 1 else next(iter(rs))
        if group == UNKNOWN:
            v.ok = False
            v.reasons.append("an action this adapter does not know")
            continue
        rating = prefs.rating(group)
        if group in prefs.off:
            v.ok = False
            v.reasons.append(f"{GROUPS[group]} is switched off for her")
        elif rating == "boundary":
            v.ok = False
            v.reasons.append(f"{GROUPS[group]} is a boundary")
        elif rating == "dislikes":
            v.reasons.append(f"she dislikes {GROUPS[group]}")
        scores.append(prefs.interest.get(group, 50.0))
    v.score = round(sum(scores) / len(scores), 1) if scores else 50.0
    return v


def compatible(framework: Framework, prefs: Prefs, her_sex: str = "female", partner_sexes: tuple[str, ...] = ("male",),
               limit: int = 10, include_disliked: bool = False) -> list[Verdict]:
    """Installed scenes for her and these partners that cross none of her boundaries, best first."""
    want = sorted((her_sex,) + tuple(partner_sexes))
    out = []
    for scene in framework.scenes().values():
        if sorted(s for s in scene.actors) != want:
            continue
        v = judge(framework, prefs, scene, her_sex)
        if not v.ok or not v.groups:
            continue
        if not include_disliked and any(r.startswith("she dislikes") for r in v.reasons):
            continue
        out.append(v)
    out.sort(key=lambda v: (-v.score, v.scene_id))
    return out[:limit]
