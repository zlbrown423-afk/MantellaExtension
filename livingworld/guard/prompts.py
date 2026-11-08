"""The lines guard careers add to an NPC's prompt: plain facts and what the rules decided. The NPC's
own personality decides how to say it; nothing here tells the NPC what the player will answer."""
from __future__ import annotations

from livingworld.guard import orgs as O, recruit as R, store as S
from livingworld.guard.profiles import Profile
from livingworld.memory.recall import time_ago

ENLIST_KEY = "EnlistRecruit"


def given_name(name: str, org: O.Org, p: Profile) -> str:
    return (f"People just call {name} '{org.guard_label}', but this guard's own name is {p.given_name}. "
            f"{p.given_name} gives it when asked, and fellow guards use it.")


def service_line(name: str, player: str, org: O.Org, svc: S.Service, now: float, leader_name: str | None) -> str:
    probation = ""
    if svc.status == "probation":
        left = svc.joined_gt + org.probation_days - now
        probation = (f", still on probation for about {max(1, round(left))} more day{'s' if round(left) != 1 else ''}"
                     if left > 0 else ", with probation about to end")
    elif svc.status == "suspended":
        probation = ", but currently suspended from duty"
    by = f", taken on by {leader_name}" if leader_name else ""
    return (f"{player} serves in {org.name} as {org.a_title(svc.rank_key)}{probation}. {player} joined "
            f"{time_ago(now - svc.joined_gt)}{by}." + (f" {org.rank_note}" if org.rank_note else ""))


def rank_line(name: str, player: str, org: O.Org, svc: S.Service, p: Profile) -> str:
    theirs = p.leader_rank if p.is_leader else org.generic_rank
    mine, other = org.rank_index(svc.rank_key), org.rank_index(theirs)
    if other < 0:
        return (f"{name} treats {player} as one of the guard, though {player}'s rank gives them no say over {name}; "
                f"how {name} feels about {player} is still {name}'s own.")
    if mine < other:
        return (f"As {org.a_title(theirs)}, {name} outranks {player} and may give {player} orders or advice. "
                f"How {name} feels about {player} personally is still {name}'s own.")
    if mine == other:
        return f"{name} and {player} hold the same rank; {name} treats {player} as a comrade, or not, as they see fit."
    return (f"{player} outranks {name}, who owes {player} the courtesy due to {org.a_title(svc.rank_key)} but is no "
            f"servant and keeps their own judgement and feelings.")


def recruit_lines(name: str, player: str, d: R.Decision, recruiters: list[str], is_recruiter: bool,
                  thane: bool) -> list[str]:
    org = d.org
    if not is_recruiter:
        who = " or ".join(recruiters) if recruiters else "the guard's officers"
        return [f"If {player} asks about joining {org.name}, {name} can tell them what the work is like but cannot "
                f"take anyone on; that is for {who}."]
    if d.ok:
        how = {"thane": f" As thane of {org.hold}, {player} is taken on at the Jarl's word.",
               "sponsor": " Someone {name} trusts here has vouched for them.".format(name=name)}.get(d.path or "", "")
        out = [f"If {player} asks to join {org.name}, {name} is willing to take them on as a "
               f"{org.ranks[0].title} on probation for {org.probation_days:g} days.{how} {name} may ask a question or "
               f"two first, and expects a recruit to keep the peace, follow orders and serve the Jarl. If {name} accepts "
               f"and {player} still wants it, begin the reply with '{ENLIST_KEY}:'. A recruit gets the guard's "
               f"armour, but no authority over anyone yet."]
        if d.invite:
            reason = (f"{player} is thane of {org.hold}" if thane else f"{name} thinks well of {player}")
            out.append(f"{reason}; if it fits the moment, {name} may suggest {player} join {org.name}.")
        return out
    reasons = "; ".join(c.why for c in d.blocking)
    return [f"If {player} asks to join {org.name}, {name} will not take them on now ({reasons}). {name} says no in "
            f"their own way and may say what would change their mind. Do not use {ENLIST_KEY}."]


def other_org_line(name: str, player: str, org: O.Org, svc: S.Service) -> str:
    return (f"{player} serves in {org.name} as {org.a_title(svc.rank_key)}. That rank carries no authority with "
            f"{name}'s own guard.")
