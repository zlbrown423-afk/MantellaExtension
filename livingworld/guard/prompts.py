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
        boss = org.superior(org.ranks[0].key)
        if boss is not None:
            out[0] += (f" A recruit takes duties and wages from {boss.name}" +
                       (" (that is {name} themself)." if boss.name == name else f", not from {name}.").format(name=name))
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


# ---- G2: duties, reports, record, pay, quarters ----

REPORT_KEY = "ReportDuty"
PAY_KEY = "PayWages"


def hours_text(days: float) -> str:
    h = max(0.0, days * 24)
    if h < 1:
        return "less than an hour"
    return f"about {round(h)} hour{'s' if round(h) != 1 else ''}"


def clock(gt: float) -> str:
    """Game time (days.fraction) as a clock time, e.g. '1:40 pm'."""
    minutes = int(round((gt % 1.0) * 24 * 60)) % (24 * 60)
    h, m = divmod(minutes, 60)
    return f"{(h % 12) or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


HOUR_WORDS = ("twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven")


def spoken_time(gt: float) -> str:
    """Game time as words for a prompt, e.g. 'half past nine in the evening'.

    Prompts never carry '9:41 pm': the model copies it, and Mantella reads 'Your next duty begins at 9:'
    as a line addressed to a character called 'Your next duty begins at 9', and drops it."""
    quarters = int(round((gt % 1.0) * 24 * 4)) % (24 * 4)
    h, q = divmod(quarters, 4)
    if q == 0 and h == 0:
        return "midnight"
    if q == 0 and h == 12:
        return "noon"
    part = ("at night" if h < 5 or h >= 22 else "in the morning" if h < 12
            else "in the afternoon" if h < 18 else "in the evening")
    if q == 3 and h in (11, 23):
        return f"a quarter to {'noon' if h == 11 else 'midnight'}"
    hour = HOUR_WORDS[((h + 1) if q == 3 else h) % 12]
    said = {0: hour, 1: f"a quarter past {hour}", 2: f"half past {hour}", 3: f"a quarter to {hour}"}[q]
    return f"{said} {part}"


def quarters_line(name: str, player: str, org: O.Org) -> str:
    return f"As a member of {org.name}, {player} has {org.quarters}."


def on_duty_line(name: str, player: str, task) -> str:
    return f"{player} is on duty right now: they were told to {task.title}."


def report_to_line(name: str, player: str, superior: str) -> str:
    return (f"{player}'s duties, reports and wages go through {superior}, {player}'s direct superior, not {name}. "
            f"If {player} asks {name} for a duty or their pay, {name} sends them to {superior}.")


def no_duty_line(name: str, player: str, why: str, next_at: float | None, now: float) -> str:
    when = (f" The next duty {name} could give {player} starts at {spoken_time(next_at)}; {name} may tell {player} to come "
            f"back then." if next_at is not None and next_at > now else "")
    return (f"{name} has no duty for {player} right now ({why}).{when} {name} does not invent a duty, a post, a shift "
            f"length or times; {player} is off duty until given one. Do not use {REPORT_KEY}.")


def incidents_line(name: str, org: O.Org, incidents) -> str:
    items = "; ".join(f"{i.why}" for i in incidents[:3])
    return (f"Trouble {org.name} knows of right now: {items}. {name} knows this as any guard would and may mention "
            f"it, without inventing details beyond it.")


def filed_notice(boss: str, r, d) -> str:
    gain = f" (+{d.merit:g} merit)" if r.outcome == "done" else ""
    return f"Reported to {boss}: {r.task.title if r.task else d.title}, done{gain}." + (" Commended!" if r.commended else "")


def filed_line(name: str, player: str, r) -> str:
    out = (f"{player} has just come back to {name} from their duty ({r.task.title}), and the guard's record shows it "
           f"was done properly; {name} takes the report now, may ask how it went, and acknowledges it in their own "
           f"words. The report is already recorded: do not use {REPORT_KEY}.")
    if r.commended:
        out += f" {player} has earned a commendation for a run of good service; {name} tells them so."
    return out


def duty_line(name: str, player: str, task, d, shift_end: float, now: float, *, new: bool, seen: bool) -> str:
    if d.incident_id is not None:
        how = ("fighting the bandits there" if d.need == "fight" else "going there and dealing with it")
        head = (f"{name} is sending {player} to deal with trouble: {d.title}. Why: {d.why}. What to do: "
                f"{d.instructions} There is no set shift; it is done by {how}." if new else
                f"{name} sent {player} to deal with trouble: {d.title}." +
                (f" There are signs {player} has dealt with it." if seen else ""))
        return (head + f" When {player} comes back to report, {name} hears them out; then begin the reply with "
                f"'{REPORT_KEY}:'. {name} does not decide whether it was really done; the guard's record will show it.")
    if new:
        head = (f"{name} is {player}'s superior and has a duty for {player} right now: {d.title}. "
                f"Why: {d.why}. What to do: {d.instructions} The shift lasts {hours_text(d.shift_hours / 24)}: "
                f"from now ({spoken_time(now)}) until {spoken_time(shift_end)}. {name} gives the order in their own words.")
    else:
        left = shift_end - now
        start = shift_end - d.shift_hours / 24
        when = (f"It is now {spoken_time(now)}; the shift runs from {spoken_time(start)} to {spoken_time(shift_end)}, so "
                + (f"{hours_text(left)} of it {'is' if round(left * 24) <= 1 else 'are'} left." if left > 0
                   else "it is over."))
        head = f"{name} gave {player} a duty: {d.title}. {when} Use these times, not a guess."
        if seen:
            head += f" Others have seen {player} at the post."
    return (head + f" When {player} comes back to report on this duty, {name} hears them out and asks what happened; "
            f"then begin the reply with '{REPORT_KEY}:'. {name} does not decide whether the report is true; "
            f"the guard's own record will show that.")


def record_line(name: str, player: str, rec) -> str:
    return (f"{name} knows {player}'s service record: {rec.words()}, merit {rec.merit:g}. {name} may bring it up "
            f"when it matters.")


def pay_line(name: str, player: str, pay) -> str:
    if pay.weeks == 0:
        return f"{player} has not yet served a full week, so no wages are due."
    out = f"{player} has earned {pay.earned} gold in wages so far"
    if pay.docked:
        out += f", with {pay.docked} docked for reprimands"
    out += f", and has been paid {pay.paid}."
    if pay.owed > 0:
        out += (f" {name} owes {player} {pay.owed} gold. If {player} asks for their wages, {name} pays them: "
                f"begin the reply with '{PAY_KEY}:'.")
    else:
        out += f" Nothing more is owed right now. Do not use {PAY_KEY}."
    return out
