"""Entry point the integration calls: bring the social world up to date before NPCs speak.

``refresh`` (on conversation start) seeds anyone new, flags children, appraises new events and catches
gossip up in the communities of the people in the conversation, within a time budget so the first
line is never held up for long. ``appraise_new`` (after perception records arrive) only appraises.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from livingworld.social import gossip, processor
from livingworld.social.seed import Seeder
from livingworld.store.minors import mark_children
from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

GOSSIP_ENV = "MANTELLA_LW_GOSSIP"
CATCH_UP_BUDGET_MS = 150.0


def gossip_enabled() -> bool:
    return os.environ.get(GOSSIP_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class Refresh:
    seeded: int = 0
    children_flagged: int = 0
    appraised: dict = field(default_factory=dict)
    catch_ups: list = field(default_factory=list)


class SocialWorld:
    def __init__(self, store: WorldStore) -> None:
        self.store = store
        self.seeder = Seeder()

    def appraise_new(self) -> dict:
        with self.store.transaction() as db:
            mark_children(db)
            return processor.process(db)

    def refresh(self, now_gt: float, present_npc_ids: list[int], budget_ms: float | None = CATCH_UP_BUDGET_MS) -> Refresh:
        result = Refresh()
        with self.store.transaction() as db:
            result.children_flagged = mark_children(db)
            result.seeded = self.seeder.apply(db, now_gt)
            result.appraised = processor.process(db)
            if gossip_enabled():
                for key in self._communities(db, present_npc_ids):
                    result.catch_ups.append(gossip.catch_up(db, key, now_gt, budget_ms))
        told = sum(c.tellings for c in result.catch_ups)
        if result.seeded or told or result.appraised:
            logger.log(23, f"Living World social: seeded {result.seeded}, appraised {result.appraised}, "
                           f"{told} piece(s) of news passed on in "
                           f"{', '.join(c.community for c in result.catch_ups) or 'no community'}")
        return result

    @staticmethod
    def _communities(db, npc_ids: list[int]) -> list[str]:
        """Settlements the present people belong to; if none, the settlement where they are seeded at all."""
        if not npc_ids:
            return []
        marks = ",".join("?" * len(npc_ids))
        return [r[0] for r in db.execute(
            f"SELECT DISTINCT c.key FROM membership m JOIN community c ON c.community_id = m.community_id"
            f" WHERE c.kind = 'settlement' AND m.npc_id IN ({marks}) ORDER BY c.key", tuple(npc_ids))]
