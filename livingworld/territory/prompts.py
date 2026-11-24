"""What one person knows about the camps, as lines for their prompt: source and confidence, nothing more."""
from __future__ import annotations

from livingworld.territory import store as S

OWNER_WORDS = {"thalmor": "the Thalmor", "wardens": "the Plains Wardens", "bandit": "bandits",
               "unoccupied": "nobody", "contested": "no one for certain (it is being fought over)"}
FACTION_NAMES = {"thalmor": "the Thalmor", "wardens": "the Plains Wardens"}
MAX_LINES = 4


def _ago(now: float, then: float) -> str:
    d = max(0.0, now - then)
    if d < 0.5:
        return "today"
    if d < 1.5:
        return "yesterday"
    if d < 13.5:
        return f"{int(round(d))} days ago"
    return f"{int(round(d / 7))} weeks ago"


def _size(n: int) -> str:
    return "" if n <= 0 else (" with a handful of soldiers" if n <= 5 else " with a sizeable garrison")


def lines(db, npc_id: int, name: str, now: float) -> list[str]:
    out: list[str] = []
    post = S.post_of(db, npc_id)
    if post is not None:
        who = FACTION_NAMES.get(post.faction, post.faction)
        role = "commands" if post.rank == "commander" else "serves in"
        out.append(f"{name} {role} the garrison {who} keep at {S.name_of(post.site_key)}"
                   f"{' as ' + post.rank if post.rank not in ('soldier', 'commander') else ''}"
                   f"{', in squad ' + post.squad if post.squad else ''}"
                   f"{', on ' + post.duty + ' duty' if post.duty else ''}.")
    rows = S.known(db, npc_id)
    rows.sort(key=lambda r: (-r["about_gt"]))
    for r in rows[:MAX_LINES]:
        place, owner = S.name_of(r["site_key"]), OWNER_WORDS.get(r["owner"], r["owner"])
        when = _ago(now, r["about_gt"])
        if r["origin"] == "witnessed":
            line = f"{name} saw for themselves ({when}) that {owner} hold {place}{_size(r['strength'])}."
            if r["owner"] == "unoccupied":
                line = f"{name} saw for themselves ({when}) that {place} is empty of anyone's garrison."
        elif r["origin"] == "reported":
            line = f"{name} was reported to ({when}) that {owner} hold {place}{_size(r['strength'])}."
        elif r["origin"] == "told":
            line = (f"{name} was told ({when}) that {owner} hold {place}, but did not see it and is not sure"
                    f" ({int(round(r['confidence'] * 100))}% sure).")
        else:
            line = f"{name} has only heard talk that {owner} hold {place}; it may be wrong."
        if r["contested"]:
            line += " It is said to be contested."
        out.append(line)
    if out:
        out.append("Say nothing about who holds any other camp: you do not know. Do not claim to have seen what you only heard.")
    return out
