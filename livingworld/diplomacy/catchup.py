"""Bringing diplomacy up to date at conversation start (rules only, no AI): seed figures who are here, let court
gossip run, turn gossip into heard statements, lapse overdue promises, deliver couriers, and close or open meetings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.diplomacy import commitments as C
from livingworld.diplomacy import common as K
from livingworld.diplomacy import figures as F
from livingworld.diplomacy import meetings as M
from livingworld.diplomacy import record as R

CIRCLE_BUDGET_MS = 60.0


@dataclass
class CatchUp:
    seeded: int = 0
    heard: int = 0
    lapsed: int = 0
    couriers: int = 0
    concluded: list[int] = field(default_factory=list)
    convened: list[int] = field(default_factory=list)
    gossip: int = 0

    def any(self) -> bool:
        return bool(self.seeded or self.heard or self.lapsed or self.couriers or self.concluded or self.convened
                    or self.gossip)

    def __str__(self) -> str:
        return (f"seeded {self.seeded}, heard {self.heard}, lapsed {self.lapsed}, couriers {self.couriers}, "
                f"concluded {self.concluded}, convened {self.convened}, court gossip {self.gossip}")


def run(db, now: float, present: list[int], location: str | None, conv_id: int | None,
        gossip: bool = True) -> CatchUp:
    out = CatchUp()
    timeline = K.active_timeline(db)
    for npc_id in present:
        if F.figure_of(db, npc_id) is not None:
            out.seeded += F.seed(db, npc_id, timeline)
    if gossip:
        from livingworld.social import gossip as G
        for key in F.circle_keys(db, present):
            out.gossip += G.catch_up(db, key, now, CIRCLE_BUDGET_MS).tellings
    out.heard = R.sync_heard(db)
    out.lapsed = C.lapse_due(db, now)
    out.couriers = M.deliver_couriers(db, now)
    out.concluded = M.conclude(db, conv_id)
    if conv_id is not None:
        out.convened = M.convene(db, conv_id, location, present, now)
    return out
