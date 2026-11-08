"""Whether a guard leader takes someone on: rules only, no LLM, every check explained.

``decide`` returns the path it would use and the checks behind it. The leader's prompt carries the
outcome (``prompts.py``) and the EnlistRecruit action is refused by the server unless the rules said
yes, so the model can voice a yes or a no but cannot overrule the rules.

What counts: whether the person already serves (here or in another hold), a recent dismissal from this
guard, a bounty in this hold, the leader's own view of them (trust and respect), what the leader has
heard about them (claims, by how much the leader believes each), a sponsor present in the conversation,
and thane status in this hold. Never gifts, gold or anything romance owns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.guard import orgs as O, store as S
from livingworld.store.minors import is_child
from livingworld.work.conditions import Check

HEARD_WEIGHT = 0.5      # what the leader has heard counts for this much of their view
HEARD_CAP = 0.4


@dataclass
class Decision:
    org: O.Org
    leader_id: int
    member_id: int
    path: str | None                       # 'applied', 'sponsor', 'thane' (when ok)
    standing: float
    sponsor_id: int | None
    checks: list[Check] = field(default_factory=list)
    invite: bool = False                   # the leader would raise it themselves

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def _view(db, holder: int, other: int) -> tuple[float, float, str]:
    row = db.execute("SELECT trust, respect, status FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (holder, other)).fetchone()
    return (row[0], row[1], row[2]) if row else (0.0, 0.0, "stranger")


def heard(db, holder: int, subject: int) -> tuple[float, int]:
    """(-HEARD_CAP..HEARD_CAP, how many claims) from what ``holder`` knows about ``subject``."""
    rows = db.execute("SELECT valence, magnitude, believed FROM v_claim_knowledge WHERE holder_id = ? AND subject_id = ?"
                      " AND act <> 'enlisted'", (holder, subject)).fetchall()
    total = sum(v * (0.5 + m) * (b if b is not None else 0.5) for v, m, b in rows)
    return max(-HEARD_CAP, min(HEARD_CAP, HEARD_WEIGHT * total)), len(rows)


def standing(db, leader_id: int, member_id: int) -> tuple[float, str]:
    trust, respect, status = _view(db, leader_id, member_id)
    h, n = heard(db, leader_id, member_id)
    value = max(-1.0, min(1.0, 0.5 * trust + 0.5 * respect + h))
    why = f"trust {trust:+.2f}, respect {respect:+.2f}, heard {n} thing(s) worth {h:+.2f}"
    if status in ("enemy", "rival"):
        value = min(value, -0.5)
        why += f", and they are {status}s"
    return value, why


def sponsor(db, org: O.Org, leader_id: int, member_id: int, present: list[int]) -> int | None:
    """Someone in the conversation the leader trusts, who trusts the member and speaks for this guard or hold."""
    from livingworld.guard import profiles
    best, best_trust = None, org.setting("sponsor_trust", 0.4)
    for npc_id in present:
        if npc_id in (leader_id, member_id) or is_child(db, npc_id):
            continue
        p = profiles.get(db, npc_id)
        if p is None or p.org_key != org.key:
            continue
        trust_in_member, _, _ = _view(db, npc_id, member_id)
        leader_trust, _, _ = _view(db, leader_id, npc_id)
        if trust_in_member >= best_trust and leader_trust >= 0.0:
            best, best_trust = npc_id, trust_in_member
    return best


def decide(db, org: O.Org, leader_id: int, member_id: int, now: float, *, bounty: int | None = None,
           thane: bool = False, present: list[int] | None = None) -> Decision:
    value, why = standing(db, leader_id, member_id)
    spon = sponsor(db, org, leader_id, member_id, present or [])
    d = Decision(org, leader_id, member_id, None, value, spon)
    if is_child(db, member_id):
        d.checks.append(Check("age", False, "children are never taken on"))
        return d
    mine = S.current(db, member_id)
    if mine is not None and mine.org_key == org.key:
        d.checks.append(Check("serving", False, f"already serves in {org.name}"))
    elif mine is not None:
        other = O.by_key(mine.org_key)
        d.checks.append(Check("serving", False, f"serves in {other.name if other else mine.org_key} and would have to "
                                                f"leave it first"))
    else:
        d.checks.append(Check("serving", True, "does not serve in any hold's guard"))
    dismissed = S.last_event_gt(db, "dismissed", member_id, org.key)
    redo = org.setting("redo_days", 30)
    if dismissed is not None and now - dismissed < redo:
        d.checks.append(Check("dismissed", False, f"was dismissed from {org.name} {now - dismissed:.0f} days ago"))
    if bounty is None:
        d.checks.append(Check("bounty", True, "no bounty reported"))
    else:
        d.checks.append(Check("bounty", bounty <= 0, "no bounty in the hold" if bounty <= 0
                              else f"has a bounty of {bounty} gold in {org.hold}"))
    if thane:
        d.path = "thane"
        d.checks.append(Check("standing", True, f"thane of {org.hold}: taken on at the Jarl's word ({why})"))
    else:
        need = org.setting("min_standing", -0.1)
        path = "applied"
        if spon is not None:
            need -= org.setting("sponsor_bonus", 0.25)
            path = "sponsor"
        ok = value >= need
        d.path = path
        d.checks.append(Check("standing", ok, f"standing {value:+.2f} (needs {need:+.2f}{' with a sponsor' if spon else ''}): {why}"))
    d.invite = (thane or value >= org.setting("invite_standing", 0.35)) and d.ok
    return d


def explain(d: Decision) -> list[str]:
    lines = [f"{d.org.name}: {'would take them on' if d.ok else 'would not take them on'}"
             + (f" ({d.path})" if d.ok and d.path else "") + (", and would invite them" if d.invite else "")]
    lines += [f"  {'ok ' if c.ok else 'NO '} {c.name}: {c.why}" for c in d.checks]
    return lines
