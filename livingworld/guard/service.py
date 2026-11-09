"""What the integration calls: note who is a guard, add guard lines to prompts, react to EnlistRecruit,
list leaders who would invite the player, and run the in-conversation test commands. Every method opens
its own transaction."""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.guard import debug as D, duty as DU, incident as INC, orgs as O, prompts as P, profiles, recruit as R, store as S
from livingworld.guard.profiles import GuardFacts
from livingworld.social import claims, processor
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

ENLIST = "mantella_lw_guard_enlist"
REPORT = "mantella_lw_guard_report"
PAY = "mantella_lw_guard_pay"
ACTIONS = (ENLIST, REPORT, PAY)
INVITE_GAP_DAYS = 7.0


@dataclass
class Scene:
    now: float
    player_id: int | None
    player_name: str
    present: list[int]
    location: str | None = None
    facts: dict[int, GuardFacts] = field(default_factory=dict)   # npc_id -> what the game reported
    thane_of: tuple[str, ...] = ()                                # holds the game reports the player is thane of
    said: str | None = None                                       # the player's last line in this conversation


@dataclass
class Issue:
    """Items for the game to hand the player (reply key mantella_lw_guard_issue)."""
    items: list[O.Item]
    counts: list[int] | None = None       # how many of each (default 1)

    def _pairs(self) -> list[tuple[O.Item, int]]:
        counts = self.counts or [1] * len(self.items)
        return [(i, c) for i, c in zip(self.items, counts) if i.form_id and c > 0]

    def key_value(self) -> str:
        return ";".join(f"{i.name}|{i.plugin}|{i.form_id}" + (f"|{c}" if c != 1 else "") for i, c in self._pairs())

    def form_ids(self) -> list[int]:
        return [int(i.form_id, 16) for i, _ in self._pairs()]

    def plugins(self) -> list[str]:
        return [i.plugin for i, _ in self._pairs()]

    def item_counts(self) -> list[int]:
        return [c for _, c in self._pairs()]


class Guard:
    def __init__(self, store: WorldStore) -> None:
        self.store = store
        self._assigned: set[int] = set()     # duties given in the conversation now running
        self.notices: list[str] = []          # on-screen notes for the player (shift started / over)

    def take_notices(self) -> list[str]:
        out, self.notices = self.notices, []
        return out

    # ---- who is who ----
    def note(self, npc_id: int, facts: GuardFacts | None, gt: float) -> profiles.Profile | None:
        with self.store.transaction() as db:
            return profiles.note(db, npc_id, facts, gt)

    # ---- helpers ----
    @staticmethod
    def _thane(db, org: O.Org, scene: Scene) -> bool:
        if org.hold in scene.thane_of:
            return True
        return S.last_event_gt(db, "thane", scene.player_id, org.key) is not None if scene.player_id else False

    @staticmethod
    def _bounty(org: O.Org, scene: Scene) -> int | None:
        found = [f.bounty for f in scene.facts.values()
                 if f.bounty is not None and O.by_crime_faction(f.crime_plugin, f.crime_faction) is org]
        return max(found) if found else None

    def _decide(self, db, org: O.Org, leader_id: int, scene: Scene) -> R.Decision:
        return R.decide(db, org, leader_id, scene.player_id, scene.now, bounty=self._bounty(org, scene),
                        thane=self._thane(db, org, scene), present=scene.present)

    @staticmethod
    def _name(db, npc_id: int | None) -> str | None:
        if npc_id is None:
            return None
        row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
        return row[0] if row else None

    # ---- prompts ----
    def lines_for(self, npc_id: int, name: str, scene: Scene) -> list[str]:
        if scene.player_id is None or npc_id == scene.player_id:
            return []
        with self.store.transaction() as db:
            if is_child(db, npc_id):
                return []
            return self._lines(db, npc_id, name, scene)

    def _lines(self, db, npc_id: int, name: str, scene: Scene) -> list[str]:
        p = profiles.get(db, npc_id)
        out: list[str] = []
        player = scene.player_name
        svc = S.current(db, scene.player_id)
        if p is None:
            return out
        org = O.by_key(p.org_key)
        if org is None:
            return out
        if p.given_name:
            out.append(P.given_name(name, org, p))
        if svc is not None and svc.org_key == org.key:
            out.append(P.service_line(name, player, org, svc, scene.now, self._name(db, svc.recruiter_id)))
            out.append(P.rank_line(name, player, org, svc, p))
            out += self._duty_lines(db, npc_id, name, org, svc, scene)
            return out
        if svc is not None:
            other = O.by_key(svc.org_key)
            if other is not None:
                out.append(P.other_org_line(name, player, other, svc))
            return out
        d = self._decide(db, org, npc_id, scene)
        recruiters = [x.name for x in org.leaders if x.recruits]
        out += P.recruit_lines(name, player, d, recruiters, p.recruits, self._thane(db, org, scene))
        if p.recruits and d.invite:
            last = S.last_event_gt(db, "invited", scene.player_id, org.key, npc_id)
            if last is None or scene.now - last >= INVITE_GAP_DAYS:
                S.event(db, "invited", [scene.player_id, npc_id], {"org": org.key, "path": d.path}, scene.now,
                        scene.location, witnesses=[])
        return out

    def _duty_lines(self, db, npc_id: int, name: str, org: O.Org, svc: S.Service, scene: Scene) -> list[str]:
        """Duties, reports, pay and the record: from the member's superior; quarters and duty from anyone."""
        player = scene.player_name
        out = [P.quarters_line(name, player, org)] if org.quarters else []
        trouble = INC.open_incidents(db, org, scene.now)
        if trouble:
            out.append(P.incidents_line(name, org, trouble))
        boss = DU.superior_id(db, org, svc)
        task = DU.in_hand(db, svc.member_id)
        if npc_id != boss:
            if task is not None:
                out.append(P.on_duty_line(name, player, task))
            person = org.superior(svc.rank_key)
            if person is not None:
                out.append(P.report_to_line(name, player, person.name))
            return out
        checks = DU.checks(db, org, svc, scene.now) if task is None else []
        if task is None and all(c.ok for c in checks):
            prev = DU.last(db, svc.member_id)
            incs = INC.for_member(db, org, svc, scene.now)
            d = DU.incident_duty(incs[0]) if incs else DU.pick(org, svc, prev.template if prev else None, scene.now)
            if d is None:
                nxt = DU.next_start(org, svc, scene.now)
                out.append(P.no_duty_line(name, player, f"no duty for {org.a_title(svc.rank_key)} at this hour",
                                          nxt, scene.now))
            if d is not None:
                task = DU.assign(db, org, svc, npc_id, d, scene.now,
                                 witnesses=[i for i in scene.present if i != svc.member_id])
                self._assigned.add(task.task_id)
                boss_name = self._name(db, npc_id) or "your superior"
                if d.incident_id is not None:
                    self.notices.append(f"Trouble: {d.title}. " + ("Fight them there" if d.need == "fight" else
                                        "Go there") + f", then report back to {boss_name}.")
                else:
                    self.notices.append(f"Duty: {d.title}. Shift: {P.hours_text(d.shift_hours / 24)}. "
                                        f"Be seen at your post, then report back to {boss_name}.")
        elif task is None:
            failed = [c for c in checks if not c.ok]
            rested_at = None
            if any(c.name == "rested" for c in failed):
                prev = DU.last(db, svc.member_id)
                rested_at = (prev.state_gt or prev.created_gt) + DU.rules(org.key).rest_hours / 24.0
            nxt = DU.next_start(org, svc, scene.now, rested_at) if svc.status in ("probation", "active") else None
            out.append(P.no_duty_line(name, player, "; ".join(c.why for c in failed), nxt, scene.now))
        if task is not None:
            d = DU.duty_for(db, task)
            if d is not None:
                logger.log(23, f"Living World guard: duty {task.task_id} '{d.key}' given day "
                               f"{DU.assigned_gt(db, task):.4f} ({P.clock(DU.assigned_gt(db, task))}), ends "
                               f"{P.clock(DU.shift_end(db, task, d))}, now day {scene.now:.4f} ({P.clock(scene.now)}), "
                               f"seen {DU.seen_count(db, task)}, new={task.task_id in self._assigned}")
                done = DU.fought_count(db, task) if d.need == "fight" else DU.seen_count(db, task)
                out.append(P.duty_line(name, player, task, d, DU.shift_end(db, task, d), scene.now,
                                       new=task.task_id in self._assigned, seen=done > 0))
        out.append(P.record_line(name, player, DU.record(db, svc)))
        if org.pay:
            out.append(P.pay_line(name, player, DU.pay(db, org, svc, scene.now)))
        return out

    # ---- upkeep, once per conversation ----
    def refresh(self, scene: Scene) -> dict[str, list[int]]:
        """Missed duties, probation, and whether the player is at their post right now."""
        self._assigned = set()
        self.notices = []
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id) if scene.player_id is not None else None
            org = O.by_key(svc.org_key) if svc else None
            result = DU.upkeep(db, scene.now)
            if org is not None and svc.status in ("probation", "active"):
                result["incidents"] = [i.event_id for i in INC.spawn(db, org, scene.now, since=svc.joined_gt)]
                result["cold"] = INC.expire(db, org, scene.now)
            boss = DU.superior_id(db, org, svc) if org else None
            # reporting to the superior is not standing the post: only other conversations count as sightings
            held = DU.in_hand(db, svc.member_id) if svc is not None else None
            first = held is not None and DU.seen_count(db, held) == 0
            if svc is not None and (boss is None or boss not in scene.present):
                seen = DU.mark_seen(db, scene.player_id, scene.location, scene.now,
                                    witnesses=[i for i in scene.present if i != scene.player_id])
                if seen is not None:
                    result.setdefault("seen", []).append(seen.task_id)
                    if first:
                        self.notices.append(f"On duty: seen at your post ({scene.location}).")
            held = DU.in_hand(db, svc.member_id) if svc is not None else None
            d = DU.duty_for(db, held) if held is not None and org else None
            if d is not None and d.incident_id is not None:
                done = (DU.fought_count(db, held) if d.need == "fight" else DU.seen_count(db, held)) > 0
                if done:
                    self.notices.append(f"Dealt with: report back to {self._name(db, boss) or 'your superior'}.")
            elif d is not None:
                end = DU.shift_end(db, held, d)
                boss_name = self._name(db, boss) or "your superior"
                half = DU.assigned_gt(db, held) + d.shift_hours / 48.0
                if scene.now >= end:
                    self.notices.append(f"Shift over: report back to {boss_name}.")
                elif scene.now >= half and DU.seen_count(db, held) > 0:
                    self.notices.append(f"You may report back to {boss_name} now "
                                        f"({P.hours_text(end - scene.now)} of the shift left).")
            processor.process(db, {"appraisal": claims.appraise})
        return result

    def leader_for(self, npc_id: int, scene: Scene) -> int:
        """``npc_id`` if they can take people on; else the only leader present who can (the action means them)."""
        with self.store.transaction() as db:
            p = profiles.get(db, npc_id)
            if p is not None and p.recruits:
                return npc_id
            able = [i for i in scene.present if (q := profiles.get(db, i)) is not None and q.recruits]
            return able[0] if len(able) == 1 else npc_id

    # ---- actions ----
    def superior_for(self, npc_id: int, scene: Scene) -> int:
        """``npc_id`` if they are the player's superior; else the superior, if present (the action means them)."""
        if scene.player_id is None:
            return npc_id
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            boss = DU.superior_id(db, org, svc) if org else None
        return boss if boss is not None and (boss == npc_id or boss in scene.present) else npc_id

    def action(self, identifier: str, npc_id: int, scene: Scene) -> tuple[str, Issue | None]:
        """Handle a guard action by ``npc_id``. Returns (what happened, items to hand out)."""
        if identifier in (REPORT, PAY) and scene.player_id is not None:
            return self._duty_action(identifier, npc_id, scene)
        if identifier != ENLIST or scene.player_id is None:
            return "not a guard action", None
        with self.store.transaction() as db:
            p = profiles.get(db, npc_id)
            org = O.by_key(p.org_key) if p else None
            if org is None or not p.recruits:
                return "this NPC cannot take anyone on", None
            d = self._decide(db, org, npc_id, scene)
            if not d.ok:
                return "refused by the rules: " + "; ".join(c.why for c in d.blocking), None
            first = org.ranks[0].key
            svc = S.enlist(db, org.key, scene.player_id, d.path or "applied", scene.now, rank_key=first,
                           recruiter_id=npc_id, sponsor_id=d.sponsor_id, location=scene.location,
                           witnesses=[i for i in scene.present if i != scene.player_id])
            processor.process(db, {"appraisal": claims.appraise})   # news at once
            return f"service {svc.service_id}: {org.name}, {org.title(first)} ({d.path})", Issue(list(O.uniform_for(org, first)))

    def _duty_action(self, identifier: str, npc_id: int, scene: Scene) -> tuple[str, Issue | None]:
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None:
                return "the player does not serve", None
            if DU.superior_id(db, org, svc) != npc_id:
                return "this NPC is not the player's superior", None
            witnesses = [i for i in scene.present if i != scene.player_id]
            if identifier == REPORT:
                r = DU.report(db, org, svc, npc_id, scene.now, scene.said, location=scene.location,
                              witnesses=witnesses)
                processor.process(db, {"appraisal": claims.appraise})
                if r.task is None:
                    return "no duty to report on", None
                return f"duty {r.task.task_id} report: {r.outcome}" + (" (commended)" if r.commended else ""), None
            amount = DU.pay_out(db, org, svc, npc_id, scene.now, witnesses=witnesses)
            if amount <= 0 or org.gold is None:
                return "nothing owed", None
            return f"paid {amount} gold", Issue([org.gold], [amount])

    # ---- the barracks bunk ----
    def bed_change(self, scene: Scene) -> tuple[O.Item, bool] | None:
        """The bed the game should give the player (True) or take back (False) now, once each; else None."""
        if scene.player_id is None:
            return None
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            for org in O.orgs():
                if org.bed is None:
                    continue
                granted = S.last_event_gt(db, "bed_granted", scene.player_id, org.key)
                revoked = S.last_event_gt(db, "bed_revoked", scene.player_id, org.key)
                serving = svc is not None and svc.org_key == org.key and svc.status in ("probation", "active")
                if serving and (granted is None or granted < svc.joined_gt or (revoked or -1) > granted):
                    S.event(db, "bed_granted", [scene.player_id], {"org": org.key}, scene.now, None, [])
                    return org.bed, True
                if not serving and granted is not None and (revoked is None or revoked < granted):
                    S.event(db, "bed_revoked", [scene.player_id], {"org": org.key}, scene.now, None, [])
                    return org.bed, False
        return None

    # ---- the game's approach step ----
    def wants_a_word(self, scene: Scene) -> list[int]:
        """Leaders met before (not in this conversation) who would invite the player to join, and have not
        in the last week: the game may have one come over (the same approach list as tasks)."""
        if scene.player_id is None:
            return []
        out = []
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            if svc is not None:
                org = O.by_key(svc.org_key)
                boss = DU.superior_id(db, org, svc) if org else None
                if boss is None or boss in scene.present or not all(c.ok for c in DU.checks(db, org, svc, scene.now)):
                    return []
                prev = DU.last(db, svc.member_id)
                return [boss] if DU.pick(org, svc, prev.template if prev else None, scene.now) else []
            leaders = [r[0] for r in db.execute("SELECT npc_id FROM guard_profile ORDER BY npc_id")]
            for npc_id in leaders:
                p = profiles.get(db, npc_id)
                org = O.by_key(p.org_key) if p else None
                if org is None or not p.recruits or npc_id in scene.present:
                    continue
                last = S.last_event_gt(db, "invited", scene.player_id, org.key, npc_id)
                if (last is None or scene.now - last >= INVITE_GAP_DAYS) and self._decide(db, org, npc_id, scene).invite:
                    out.append(npc_id)
        return out

    # ---- test commands ----
    def debug(self, text: str, scene: Scene) -> str | None:
        cmd = D.parse(text)
        if cmd is None or scene.player_id is None:
            return None
        with self.store.transaction() as db:
            org = O.by_key(cmd.org_key) if cmd.org_key else None
            if org is None:
                for npc_id in scene.present:
                    p = profiles.get(db, npc_id)
                    if p is not None:
                        org = O.by_key(p.org_key)
                        break
            svc = S.current(db, scene.player_id)
            if org is None and svc is not None:
                org = O.by_key(svc.org_key)
            org = org or O.orgs()[0]
            return run(db, cmd, org, scene.player_id, scene.now, scene.location)


def run(db, cmd: D.Command, org: O.Org, player_id: int, now: float, location: str | None) -> str:
    """Apply one test command (shared by the in-game debug line and the developer CLI)."""
    svc = S.current(db, player_id)
    if cmd.verb == "status":
        if svc is None:
            return "not serving"
        o = O.by_key(svc.org_key)
        return (f"{o.name if o else svc.org_key}: {o.title(svc.rank_key) if o else svc.rank_key}, {svc.status}, "
                f"joined day {svc.joined_gt:.2f} ({svc.path}), merit {svc.merit:g}")
    if cmd.verb == "thane":
        S.event(db, "thane", [player_id], {"org": org.key, "hold": org.hold}, now, location, source="admin")
        return f"player counts as thane of {org.hold}"
    if cmd.verb == "enlist":
        if svc is not None:
            return f"already serving ({svc.org_key})"
        svc = S.enlist(db, org.key, player_id, "admin", now, rank_key=org.ranks[0].key, location=location,
                       source="admin", reason="developer: enlisted")
        return f"enlisted in {org.name} as {org.title(svc.rank_key)}"
    if cmd.verb == "reinstate":
        if svc is not None:
            return "already serving"
        last = S.history(db, player_id, org.key)
        if not last:
            return f"never served in {org.name}"
        svc = S.change(db, last[-1], "developer: reinstated", now, status="active", kind="reinstated",
                       location=location, source="admin")
        return f"reinstated in {org.name} as {org.title(svc.rank_key)}"
    if svc is None:
        return "not serving"
    o = O.by_key(svc.org_key) or org
    if cmd.verb == "rank":
        rank = next((r.key for r in o.ranks if cmd.rank in (r.key, r.title.lower())), None)
        if rank is None:
            return f"{o.name} has no rank {cmd.rank!r}"
        up = o.rank_index(rank) > o.rank_index(svc.rank_key)
        svc = S.change(db, svc, "developer: rank set", now, rank_key=rank, status="active",
                       kind="promoted" if up else "demoted", location=location, source="admin")
        return f"rank now {o.title(svc.rank_key)}"
    if cmd.verb == "dismiss":
        S.change(db, svc, "developer: dismissed", now, status="dismissed", kind="dismissed", location=location,
                 source="admin")
        return f"dismissed from {o.name}"
    return _run_duty(db, cmd, o, svc, now, location)


def _run_duty(db, cmd: D.Command, org: O.Org, svc: S.Service, now: float, location: str | None) -> str:
    boss = DU.superior_id(db, org, svc)
    person = org.superior(svc.rank_key)
    if cmd.verb == "record":
        rec = DU.record(db, svc)
        return f"record: {rec.words()}, merit {rec.merit:g}"
    if cmd.verb == "pay":
        p = DU.pay(db, org, svc, now)
        return f"pay: {p.weeks} weeks, earned {p.earned}, docked {p.docked}, paid {p.paid}, owed {p.owed}"
    if cmd.verb == "probation":
        if svc.status != "probation":
            return f"not on probation ({svc.status})"
        result = DU.settle_probation(db, org, svc, max(now, svc.joined_gt + org.probation_days))
        return f"probation: {result or 'still running'}"
    if cmd.verb == "incident":
        kinds = INC.kinds(org.key)
        k = next((k for k in kinds if cmd.arg and cmd.arg in k.key), kinds[0] if kinds else None)
        if k is None:
            return "no incidents configured"
        inc = INC._open(db, org, k, int(now), now, INC._roll(org.key, now, k.key))
        return f"incident {inc.event_id}: {inc.title} (talk to {person.name if person else 'your superior'})"
    if boss is None:
        who = person.name if person else "a superior"
        return f"{who} is not known to the world yet; talk to them once first"
    held = DU.in_hand(db, svc.member_id)
    if cmd.verb == "duty":
        if held is not None:
            return f"already on duty: {held.title}"
        prev = DU.last(db, svc.member_id)
        d = DU.pick(org, svc, prev.template if prev else None, now, any_hour=True)
        if d is None:
            return "no duty fits this rank"
        task = DU.assign(db, org, svc, boss, d, now, source="admin")
        return f"duty {task.task_id}: {d.title} ({d.shift_hours:g} h, report to {person.name if person else 'superior'})"
    if held is None:
        return "no duty in hand"
    if cmd.verb == "seen":
        d = DU.duty_for(db, held)
        if d is not None and d.need == "fight":
            DU.mark_fought(db, svc.member_id, d.where[0] if d.where else location, now)
            return f"fought at the place of: {held.title}"
        DU.mark_seen(db, svc.member_id, location, now, force=True)
        return f"seen at the post of: {held.title}"
    r = DU.report(db, org, svc, boss, now, "(developer report)", location=location, source="admin")
    return f"report: {r.outcome}" + (" (commended)" if r.commended else "")
