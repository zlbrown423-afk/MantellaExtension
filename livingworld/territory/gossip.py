"""News of a site spreads by word of mouth, slowly and thinly: nobody is told everything.

``spread`` runs lazily at conversation start (like the other catch-ups). A change at least ``DELAY_DAYS`` old
reaches a person who lives where that site's news goes (``news_places`` matched on where they live), by a seeded
roll, as a ``rumor`` of lower confidence and with a named teller who already knew it. It never overwrites
something a person saw themselves, and each person gets one copy of each change.
"""
from __future__ import annotations

import hashlib

from livingworld.territory import store as S
from livingworld.territory.sites import by_key

DELAY_DAYS = 1.0
REACH_DAYS = 6.0          # each person hears within this many days of the change
CATCH_UP_DAYS = 30.0      # a catch-up still delivers changes this old (nobody talked for a while), then stops
CHANCE = 0.55
MAX_PER_RUN = 40


def _roll(*parts) -> float:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16) / float(16 ** 12)


def spread(db, now: float) -> int:
    told = 0
    tl = S.timeline(db)
    for ch in db.execute("SELECT * FROM v_territory_change WHERE game_time <= ? AND game_time >= ? ORDER BY change_id",
                         (now - DELAY_DAYS, now - CATCH_UP_DAYS)):
        site = by_key(ch["site_key"])
        if site is None or not site.news_places:
            continue
        tellers = [r["holder_id"] for r in db.execute(
            "SELECT holder_id FROM v_territory_knowledge WHERE change_id = ? AND origin IN ('witnessed','reported')"
            " ORDER BY holder_id", (ch["change_id"],))]
        if not tellers:
            continue   # nobody saw it, so nobody has anything to pass on yet
        marks = ",".join("?" for _ in site.news_places)
        for r in db.execute(f"SELECT npc_id FROM npc WHERE is_child = 0 AND is_player = 0 AND home_location IN ({marks})"
                            " ORDER BY npc_id", site.news_places):
            npc = r["npc_id"]
            if told >= MAX_PER_RUN:
                return told
            if _roll(tl, ch["change_id"], npc) > CHANCE:
                continue
            teller = tellers[int(_roll(tl, ch["change_id"], npc, "who") * len(tellers)) % len(tellers)]
            when = ch["game_time"] + DELAY_DAYS + _roll(tl, ch["change_id"], npc, "when") * (REACH_DAYS - DELAY_DAYS)
            if when > now:
                continue
            conf = 0.5 if (site.weight >= 0.5) else 0.4
            if S.learn(db, npc, ch["site_key"], ch["owner_after"], ch["strength"], bool(ch["contested"]), "told", conf,
                       ch["game_time"], when, ch["change_id"], told_by=teller):
                told += 1
    return told
