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
               f"and {player} still wants it, begin the reply with '{ENLIST_KEY}:'. That hands {player} the guard's "
               f"armour, helmet, shield and sword then and there, so there is nothing to fetch from the barracks and "
               f"no reason to put it off. A recruit has no authority over anyone yet."]
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


def complaint_line(name: str, player: str, c, *, player_on_it: bool, player_dealt: bool, guard: str,
                   ago: str) -> str:
    """For the townsperson who reported an incident (``incident.Complaint``): they remember doing so."""
    inc = c.incident
    account = inc.complaint or inc.why
    out = (f"{name} went to {guard} {ago} to report trouble, and remembers it well: {account}")
    if inc.look:
        out += (f" All {name} saw of the culprit was {inc.look}; {name} does not know their name and makes up no "
                f"other description.")
    elif inc.suspect:
        out += f" {name} thinks it was {inc.suspect}, from what folk are saying."
    if c.state == "open":
        if player_on_it:
            out += (f" {player} is the guard sent to deal with it: {name} recognises that this is about the "
                    f"complaint, is glad someone has come, and readily tells {player} what happened, what was lost "
                    f"and what they saw, to help find the culprit.")
        else:
            out += f" Nobody has been caught yet; {name} is still waiting on the guard and is impatient about it."
    elif c.state in ("arrested", "cleared"):
        out += (f" The guard has since dealt with it ({'the culprit was caught' if c.state == 'arrested' else 'it is over'})"
                + (f", and it was {player} who did it: {name} is grateful to {player} for it." if player_dealt else "."))
    else:
        out += f" Nobody was ever caught for it, and {name} is sore that the guard let it go cold."
    return out


def filed_notice(boss: str, r, d, summary=None) -> str:
    gain = f" (+{d.merit:g} merit)" if r.outcome == "done" else ""
    shift = f" Shift: {summary.counts()}." if summary is not None else ""
    return (f"Reported to {boss}: {r.task.title if r.task else d.title}, done{gain}.{shift}"
            + (" Commended!" if r.commended else ""))


def filed_line(name: str, player: str, r, summary=None) -> str:
    out = (f"{player} has just come back to {name} from their duty ({r.task.title}), and the guard's record shows it "
           f"was done properly; {name} takes the report now, may ask how it went, and acknowledges it in their own "
           f"words. The report is already recorded: do not use {REPORT_KEY}.")
    if summary is not None:
        out += (f" What {player} has to report from the shift (from the guard's record; {player} tells it in their "
                f"own words and {name} may ask about any of it): {summary.text()}")
        if summary.attention:
            out += (f" {name} decides what to do about the matters for the superior: deal with them, pass them up, "
                    f"or tell {player} to follow one up.")
        if summary.quiet:
            out += f" A quiet shift is rare; {name} may remark on it."
    if r.commended:
        out += f" {player} has earned a commendation for a run of good service; {name} tells them so."
    if getattr(r, "trained", False):
        out += (f" That was the last step of {player}'s recruit training: {name} tells them they have finished it "
                f"(with no reprimand on their record, that also ends their probation).")
    if not (r.task and r.task.template and r.task.template.startswith("duty:training:")):
        out += " " + written_report_line(name, player)
    return out


def encounter_line(name: str, player: str, org: O.Org, svc: S.Service, enc, *, is_superior: bool,
                   is_guard: bool) -> str:
    """Why ``name`` came over to the player on their shift (G2e): the reason, and who outranks whom."""
    title = org.title(svc.rank_key)
    if is_superior:
        head = (f"{name} has come over to {player}, their subordinate ({title}), who is on duty: {enc.reason}. "
                f"{name} speaks as the superior: brief, expects answers, may give an instruction.")
    elif is_guard:
        head = (f"{name}, a fellow member of {org.name}, has come over to {player} ({title}), who is on duty, because "
                f"{enc.reason}. {name} speaks to them as one guard to another, minding rank.")
    else:
        head = (f"{name} has come over to {player}, a {title} of {org.name} on duty, because {enc.reason}. This is why "
                f"{name} is talking to them: {name} raises it in their own first words, as fits who they are and what "
                f"they remember of {player}. It is an everyday matter, not an emergency unless it says so.")
    return (head + f" {player} may help, send them elsewhere, or say they will take it to their superior; {name} "
            f"reacts as they would. {name} does not invent a crime, a fight or danger beyond this.")


def training_line(name: str, player: str, d, step_no: int, steps: int, *, new: bool, seen: bool) -> str:
    """G2g: a recruit training step (not the quiz, which has its own line)."""
    how = {"fight": f"a sparring bout with fists against a fellow guard ({player} asks one; it is a brawl, not a "
                    f"real fight)", "escort": "walking the patrol with a senior guard beside them",
           "seen": "being seen at the drill"}.get(d.need, "doing it")
    if new:
        head = (f"{name} is training {player}, a new recruit (step {step_no} of {steps}): {d.title}. Why: {d.why}. "
                f"What to do: {d.instructions} It is done by {how}. {name} explains it in their own words.")
    else:
        head = f"{name} set {player} a training step (step {step_no} of {steps}): {d.title}."
        if seen:
            head += f" There are signs {player} has done it."
    return (head + f" When {player} comes back to report, {name} hears them out; then begin the reply with "
            f"'{REPORT_KEY}:'. The guard's record shows whether it was really done.")


def spar_line(name: str, player: str) -> str:
    """G2g: a fellow guard, while the player's training step is a fist bout with one of them."""
    return (f"{player} is a recruit whose training step now is a sparring bout, fists only, with a fellow guard. If "
            f"{player} asks {name} for a bout, {name} agrees (friendly training, no hard feelings, no steel) and says "
            f"so plainly, for example 'Put up your fists' or 'Let's spar', and begins the reply with 'Brawl:'.")


def report_note(name: str, player: str, title: str, outcome: str, trained: bool, missing: str | None) -> str:
    """G2g: the player said they are reporting back; the guard's rules already judged it."""
    if outcome == "not_ready":
        return (f"{player} says they are reporting back on their duty to {title}, but the guard's record shows it is "
                f"not done yet: {missing}. {name} says so plainly and tells {player} what is still to do; no "
                f"reprimand. Do not begin the reply with '{REPORT_KEY}:'.")
    done = (f"{player} is reporting back on their duty to {title}. The guard's record has already settled it: it was "
            f"done, and the report is accepted. {name} acknowledges it in their own words and does not begin the reply "
            f"with '{REPORT_KEY}:'.")
    if trained:
        done += f" That finishes {player}'s recruit training: {name} says so; from now on {player} gets routine duties."
    return done


def quiz_line(name: str, player: str, org: O.Org, questions, right: list[str], wrong: int, step_no: int, steps: int,
              pass_right: int) -> str:
    """G2g: the superior tests the recruit on the hold's law and the guard's orders, now."""
    todo = [q for q in questions if q.key not in right]
    items = " ".join(f"({i + 1}) {q.ask} (A good answer: {q.answer}.)" for i, q in enumerate(todo[:4]))
    return (f"Training step {step_no} of {steps}: {name} is testing {player} on the law of {org.hold} and the guard's "
            f"orders, now, in this conversation. {name} asks ONE question at a time from these, waits for the answer, "
            f"says plainly whether it was right and gives the right answer if not: {items} So far {len(right)} right, "
            f"{wrong} wrong; {pass_right} right answers pass. The guard's record keeps the score: {name} does not "
            f"announce a pass or a fail by their own judgement, and does not use {REPORT_KEY}.")


def written_report_line(name: str, player: str) -> str:
    return (f"{name} asks {player} for their report in their own words: what happened, who came to them, what they "
            f"saw and what still needs doing. It goes into the guard's ledger as {player}'s written report; {name} "
            f"listens, asks about anything left out, and may remark on how thorough it is.")


def reports_line(name: str, player: str, past: list[tuple[str, str]]) -> str:
    """Recent written reports the superior can quote back."""
    items = "; ".join(f"on '{title}': \"{text[:160]}\"" for title, text in past[:2])
    return f"{name} remembers {player}'s recent written reports: {items}."


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


def watch_line(name: str, player: str) -> str:
    return (f"While on a shift, people may come to {player} with questions, disputes and reports, and {player} "
            f"should keep their eyes open: all of it goes in the report when they come back to {name}.")


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


# ---- G2f arrests ----

def _evidence_text(ev) -> str:
    return "; ".join(e.why for e in ev)


def arrest_note(name: str, player: str, org: O.Org, decision, jail_name: str) -> str:
    """For the NPC being arrested, added to the player's line: what the rules decided they do."""
    a = decision.arrest
    if a.response == "argue":
        return (f"[{player}, of {org.name}, has just told {name} they are under arrest, but nothing in the guard's "
                f"record ties {name} to any crime. {name} does not go: {name} protests and demands to know the charge, "
                f"in {name}'s own way. If {player} insists, {name} will have to go.]")
    charge = f"for {a.charge}" if a.charge else "with no charge they can name"
    ev = (f" What the guard holds against {name}: {_evidence_text(decision.evidence)}." if decision.evidence else
          f" Nothing in the record ties {name} to a crime: this arrest is wrongful, and {name} knows it.")
    doing = {
        "submit": (f"The rules decided: {name} goes quietly (protesting is fine) and follows {player} to the cells in "
                   f"{jail_name}. {name} does not run or fight now."),
        "flee": f"The rules decided: {name} will not be taken. {name} bolts and runs from {player} right now.",
        "fight": f"The rules decided: {name} will not be taken. {name} turns on {player} and fights to get away.",
    }[a.response]
    return f"[{player}, of {org.name}, is arresting {name} {charge}.{ev} {doing}]"


def arrest_notice(name: str, decision, jail_name: str) -> str:
    a = decision.arrest
    if a.response == "argue":
        return (f"Arrest: nothing on record against {name}. Say it again to arrest them anyway "
                f"(wrongful arrest: reprimand).")
    if a.response == "submit":
        return f"Arrest: {name} goes quietly. Bring them to the cell in {jail_name}" + \
            (" (wrongful arrest)." if a.wrongful else ".")
    return f"Arrest: {name} " + ("runs for it!" if a.response == "flee" else "fights back!")


def prisoner_line(name: str, player: str, a, state: str, until: float | None, now: float, jail_name: str) -> str:
    charge = f"for {a.charge}" if a.charge else "with no charge named"
    if state == "escorting":
        return (f"{name} is under arrest {charge}, arrested by {player}, and is being taken to the cells in "
                f"{jail_name}. {name} is in {player}'s custody and does not wander off.")
    left = f" for about {hours_text(until - now)} more" if until is not None and until > now else ""
    return (f"{name} is locked in a cell in {jail_name}{left}, arrested by {player} {charge}. {name} cannot leave "
            f"and feels about {player} as {name} sees fit.")


def booked_line(name: str, player: str, bookings) -> str:
    parts = []
    for b in bookings:
        a = b.arrest
        if b.wrongful:
            parts.append(f"{a.name} (nothing against them: {name} has just had {a.name} let out and reprimands {player} "
                         f"for a wrongful arrest)")
        else:
            parts.append(f"{a.name} for {a.charge} (well done)")
    return f"{player} has just brought in and booked: " + "; ".join(parts) + "."


def prisoners_line(name: str, org: O.Org, held, jail_name: str) -> str:
    who = ", ".join(f"{a.name} ({a.charge or 'no charge'})" for a, _ in held)
    return f"In the cells of {jail_name} right now: {who}."


# ---- G3 promotions ----

def promoted_line(name: str, player: str, org: O.Org, frm: str, to: str) -> str:
    return (f"{name} has just promoted {player} from {org.title(frm)} to {org.title(to)} of {org.name}: the promotion "
            f"board found {player}'s service worthy of it. {name} tells {player} so now, in their own words, before "
            f"anything else, and may say what the new rank asks of them. It is already recorded. If {player} turns it "
            f"down, {name} accepts that, and {player} stays {org.a_title(frm)}.")


def declined_line(name: str, player: str, org: O.Org, frm: str, to: str) -> str:
    return (f"{player} has just turned down promotion to {org.title(to)} and stays {org.a_title(frm)}. {name} accepts "
            f"it, in character; the board may offer it again in time.")


_BOARD_SAY = {
    "status": "finishing probation",
    "training": "finishing recruit training",
    "clean": "a stretch without reprimands",
    "seat": "an open seat at that rank",
}


def board_line(name: str, player: str, b) -> str:
    """What the superior can tell the member about promotion: the next rank and what it still needs."""
    if b.target is None:
        return f"{player} holds the highest rank in {b.org.name}; there is no promotion above it."
    if b.ok:
        return (f"{player} has passed the promotion board for {b.target.title}; {name} will see to it the next time "
                f"they speak.")
    missing = []
    for c in b.missing[:4]:
        if c.name == "view":
            missing.append(f"{name}'s own good opinion, which {player} has not yet earned")
        elif c.name == "jarl":
            missing.append("the Jarl's favour")
        else:
            missing.append(_BOARD_SAY.get(c.name, c.why))
    return (f"If {player} asks about promotion, {name} can say plainly what the next rank, {b.target.title}, still "
            f"needs: " + "; ".join(missing) + ". Promotion comes from the guard's rules, not from asking; "
            f"{name} cannot promote {player} on the spot.")
