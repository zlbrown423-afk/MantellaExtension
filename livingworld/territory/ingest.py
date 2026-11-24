"""Apply the game's territory records: the ownership mirror, the event, and who learned it first-hand."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from livingworld.perception.records import BadRecord
from livingworld.territory import records as R, store as S
from livingworld.territory.sites import by_key

logger = logging.getLogger("livingworld.territory")

WITNESS_RANGE = 50.0          # metres: farther than this no one makes out who holds a camp
KIND_OF = {"occupied": "occupied", "reinforced": "reinforced", "cleared": "cleared", "withdrawn": "withdrawn",
           "established": "established", "contested": "contested", "abandoned": "abandoned"}
GARRISON_END = {"recall": "recalled", "dead": "dead", "withdraw": "withdrawn"}


@dataclass
class Result:
    applied: int = 0
    duplicate: int = 0
    skipped: int = 0


def _dup(db, key: str) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE game_key = ? AND type LIKE 'territory.%' LIMIT 1",
                      (key,)).fetchone() is not None or db.execute(
        "SELECT 1 FROM v_territory_change WHERE game_key = ? LIMIT 1", (key,)).fetchone() is not None or db.execute(
        "SELECT 1 FROM garrison_assignment WHERE game_key = ? LIMIT 1", (key,)).fetchone() is not None


def ingest(store, identities, raw: list[str]) -> Result:
    """Parse and store each record; a bad record is logged and skipped, never fatal."""
    out = Result()
    for text in raw:
        try:
            rec = R.parse(text)
        except (BadRecord, ValueError) as e:
            logger.error(f"Living World: territory record skipped ({e}): {text[:200]}")
            out.skipped += 1
            continue
        try:
            # people are registered first: the registry opens its own transaction
            witnesses = _witness_ids(identities, rec)
            npc = identities.ensure_npc(rec.npc.identity(), rec.game_time) if rec.npc is not None else None
            with store.transaction() as db:
                if _dup(db, rec.key):
                    out.duplicate += 1
                    continue
                if rec.is_garrison:
                    _garrison(db, npc, rec)
                else:
                    _site(db, witnesses, rec)
            out.applied += 1
        except Exception:
            out.skipped += 1
            logger.exception(f"Living World: territory record {rec.key} failed; carrying on")
    return out


def _witness_ids(identities, rec: R.TerritoryRecord) -> list[tuple[int, float]]:
    ids = []
    for w in rec.witnesses:
        if w.distance_m > WITNESS_RANGE or not w.had_los:
            continue
        npc = identities.ensure_npc(w.actor.identity(), rec.game_time)
        if npc is not None and not w.actor.is_player:
            ids.append((npc, 1.0 if w.distance_m <= 20 else 0.85))
    return ids


def _site(db, witnesses: list[tuple[int, float]], rec: R.TerritoryRecord, source: str = "game") -> None:
    site = by_key(rec.site)
    now = S.state_of(db, rec.site)
    gt = rec.game_time
    if rec.via == "sighted":   # a patrol saw a position: news only, nothing about the owner changes
        owner = rec.owner or now.owner
        S.event(db, "sighted", site, {"owner_before": now.owner, "owner_after": owner, "strength": rec.strength,
                                      "contested": rec.contested, "source": source}, gt, [i for i, _ in witnesses], source=source, game_key=rec.key)
        for npc, conf in witnesses:
            S.learn(db, npc, rec.site, owner, rec.strength, rec.contested, "witnessed", conf, gt, gt, None)
        return
    before = rec.prev or now.owner
    owner = rec.owner
    since = rec.since if rec.since is not None else (now.held_since if owner == now.owner and now.held_since else gt)
    payload = {"owner_before": before, "owner_after": owner, "strength": rec.strength, "contested": rec.contested,
               "held_since": since, "source": source}
    if rec.cooldown is not None:
        payload["cooldown_gt"] = rec.cooldown
    event_id = S.event(db, rec.via, site, payload, gt, [i for i, _ in witnesses], source=source, game_key=rec.key)
    change_id = S.add_change(db, rec.site, KIND_OF[rec.via], before, owner, rec.strength, rec.contested, since,
                             rec.cooldown if rec.cooldown is not None else
                             (now.cooldown_gt if owner == now.owner else None), gt, event_id, rec.key, source)
    for npc, conf in witnesses:
        S.learn(db, npc, rec.site, owner, rec.strength, rec.contested, "witnessed", conf, gt, gt, change_id)
    # a holding's commander and a garrison's officer hear about their own site from their own soldiers
    if owner in ("wardens", "thalmor"):
        for post in S.roster(db, rec.site, owner):
            if post.rank in ("commander", "officer"):
                S.learn(db, post.npc_id, rec.site, owner, rec.strength, rec.contested, "reported", 0.9, gt, gt, change_id)
    if owner == "unoccupied" or rec.via in ("cleared", "withdrawn", "abandoned"):
        for post in S.roster(db, rec.site):   # nobody is posted to a site nobody holds any more
            S.end(db, post.npc_id, "withdrawn" if rec.via == "withdrawn" else "dead" if rec.via == "abandoned" else "recalled",
                  gt, note=f"site {rec.via}")


def _garrison(db, npc: int | None, rec: R.TerritoryRecord) -> None:
    if npc is None:
        return
    gt = rec.game_time
    if rec.via in ("assign", "replace"):
        S.assign(db, rec.site, npc, rec.faction, gt, rank=rec.rank, squad=rec.squad, duty=rec.duty,
                 equipment=rec.equip, health=rec.hp, game_key=rec.key)
    elif rec.via == "transfer":
        S.assign(db, rec.to, npc, rec.faction, gt, rank=rec.rank, squad=rec.squad, duty=rec.duty,
                 equipment=rec.equip, health=rec.hp, game_key=rec.key)
    elif rec.via in ("recall", "dead"):
        S.end(db, npc, GARRISON_END[rec.via], gt, note=rec.duty or "", health=rec.hp if rec.via != "dead" else 0.0)
