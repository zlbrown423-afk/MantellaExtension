"""Test commands said or typed in a conversation: "debug territory <verb> [site]".

    debug territory status          every site's owner, strength and cooldown (logged)
    debug territory occupy [site]   the Thalmor take the site (the person you are talking to sees it)
    debug territory reinforce [s]   the holder's garrison grows by two
    debug territory clear [site]    the player drives the holder out (starts the reclamation cooldown)
    debug territory establish [s]   the Plains Wardens raise their standard there
    debug territory tell [site]     the person you are talking to hears of it as a rumour

Everything is written with ``source = 'admin'`` on the save timeline, so an older save undoes it. No soldier is
spawned: this only feeds the mirror, to test what people know and say. The game plugin has its own debug powers.
"""
from __future__ import annotations

import re

from livingworld.territory import store as S
from livingworld.territory.sites import all_sites, by_key

COMMANDS = ("status", "occupy", "reinforce", "clear", "establish", "tell")
_LINE = re.compile(r"^\W*debug\s+territory\b\s*(\w+)?\s*(.*)$", re.I)
COOLDOWN_DAYS = 14.0


def parse(text: str | None) -> tuple[str, str | None] | None:
    m = _LINE.match(text or "")
    if not m:
        return None
    verb = (m.group(1) or "status").lower()
    if verb not in COMMANDS:
        return None
    rest = re.sub(r"[^a-z ]", "", (m.group(2) or "").lower().replace("'", "")).strip()
    site = next((s for s in all_sites() if rest and (s.key.replace("_", " ") in rest or s.name.lower().replace("'", "") in rest)), None)
    return verb, site.key if site else None


def _default_site(db, verb: str) -> str:
    st = S.states(db)
    for s in all_sites():
        owner = st[s.key].owner if s.key in st else "unoccupied"
        if verb in ("occupy", "establish") and owner == "unoccupied":
            return s.key
        if verb in ("reinforce", "clear", "tell") and owner not in ("unoccupied", "bandit"):
            return s.key
    return all_sites()[0].key


def run(db, text: str | None, npc_id: int | None, now: float) -> str | None:
    parsed = parse(text)
    if parsed is None:
        return None
    verb, key = parsed
    if verb == "status":
        st = S.states(db)
        parts = []
        for s in all_sites():
            x = st.get(s.key)
            parts.append(f"{s.name}: {x.owner if x else 'unoccupied'}" + (f" ({x.strength})" if x and x.strength else "")
                         + (f", cooldown to {x.cooldown_gt:.1f}" if x and x.cooldown_gt and x.cooldown_gt > now else ""))
        return "; ".join(parts)
    key = key or _default_site(db, verb)
    site, cur = by_key(key), S.state_of(db, key)
    from livingworld.territory import ingest as I, records as R
    base = f"debug-{verb}-{key}-{now:.3f}"
    if verb == "tell":
        if npc_id is None or cur.owner == "unoccupied":
            return f"nothing to tell about {site.name}"
        S.learn(db, npc_id, key, cur.owner, cur.strength, cur.contested, "rumor", 0.4, cur.changed_gt or now, now, None)
        return f"{site.name}: the person you are talking to now has heard talk that {cur.owner} hold it"
    rec = R.TerritoryRecord(key=base, via={"occupy": "occupied", "reinforce": "reinforced", "clear": "cleared",
                                           "establish": "established"}[verb], game_time=now, site=key)
    if verb == "occupy":
        rec.owner, rec.prev, rec.strength = "thalmor", cur.owner, 4
    elif verb == "establish":
        rec.owner, rec.prev, rec.strength = "wardens", cur.owner, 3
    elif verb == "reinforce":
        if cur.owner not in ("thalmor", "wardens"):
            return f"{site.name} is not held by anyone to reinforce"
        rec.owner, rec.prev, rec.strength = cur.owner, cur.owner, cur.strength + 2
    else:
        if cur.owner not in ("thalmor", "wardens", "bandit"):
            return f"{site.name} is not held by anyone to clear"
        rec.owner, rec.prev, rec.strength, rec.cooldown = "unoccupied", cur.owner, 0, now + COOLDOWN_DAYS
    I._site(db, [], rec, source="admin")
    if npc_id is not None and rec.owner:
        S.learn(db, npc_id, key, rec.owner, rec.strength, False, "witnessed", 1.0, now, now,
                db.execute("SELECT MAX(change_id) FROM territory_change").fetchone()[0])
    return f"{site.name}: {rec.via}, now {rec.owner} ({rec.strength})"
