"""What the integration calls: note who is a guard, add guard lines to prompts, react to EnlistRecruit,
list leaders who would invite the player, and run the in-conversation test commands. Every method opens
its own transaction."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from livingworld.guard import arrest as AR, debug as D, duty as DU, encounter as ENC, incident as INC, reports as RP, training as TR, orgs as O, peace as PEACE, promotion as PR, prompts as P, profiles, recruit as R, store as S
from livingworld.guard.profiles import GuardFacts
from livingworld.social import claims, processor
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore
from livingworld.work import tasks as T
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
class ArrestOrder:
    """What the game does with someone the player arrested (reply keys mantella_lw_guard_arrest_*)."""
    name: str
    mode: str              # 'submit' (follows to the cells) | 'flee' | 'fight'
    arrest_id: int
    days: float
    jail: O.Item | None


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
        self.filed: DU.Report | None = None   # the report filed on coming back to the superior this conversation
        self.notices: list[str] = []          # on-screen notes for the player (shift started / over)
        self.summary: ENC.Summary | None = None   # G2e: the shift summary filed with that report
        self._encounters: dict[int, ENC.Encounter] = {}   # npc_id -> why they came over (this conversation)
        self._report_lines: list[str] = []      # what the player said after a filed report (written report)
        self._watch_soon: tuple[int, float] | None = None   # "debug guard watch": (task id, when asked)
        self.arrest_orders: list[ArrestOrder] = []   # G2f: for the game, sent with the next NPC line
        self.release_orders: list[int] = []          # G2f: arrest ids whose prisoner the game lets out now
        self.bookings: list[AR.Booking] = []         # G2f: prisoners booked with the superior this conversation
        self._first_orders: int | None = None        # service id enlisted in this conversation, first duty not given yet
        self.promoted: tuple[str, str, int] | None = None   # G3: (rank from, rank to, superior) given this conversation
        self._declined: tuple[str, str] | None = None        # G3: (from, to) turned down this conversation
        self.spar_partners: set[str] = set()   # G2g: fellow guards told the player needs a fist bout (by name)
        self._worded_report = False            # the player's latest line was judged as a report (filed or not ready)

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
        enc_line = self._encounter_line(db, npc_id, name, p, svc, scene)
        if enc_line:
            out.append(enc_line)
        out += self._prisoner_lines(db, npc_id, name, scene)
        if p is None:
            return out
        org = O.by_key(p.org_key)
        if org is None:
            return out
        stepped_in = PEACE.line(db, npc_id, name, scene.player_id, player, scene.now,
                                fellow=org.name if svc is not None and svc.org_key == org.key else None)
        if stepped_in:
            out.append(stepped_in)
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

    def _prisoner_lines(self, db, npc_id: int, name: str, scene: Scene) -> list[str]:
        if not AR.enabled():
            return []
        a = AR.held(db, npc_id, scene.now)
        if a is None:
            return []
        org = O.by_key(a.org_key)
        st = AR.status(db, a, scene.now)
        j = AR.jailed(db, a)
        return [P.prisoner_line(name, self._name(db, a.member_id) or scene.player_name, a, st,
                                float(j["until"]) if j else None, scene.now, self._jail_name(org))]

    @staticmethod
    def _jail_name(org: O.Org | None) -> str:
        return org.jail.name if org is not None and org.jail is not None else "the cells"

    def turn_notes(self, scene: Scene, text: str) -> list[str]:
        """Notes for the AI on the player's line: first orders right after enlisting, and arrests (G2f)."""
        self._worded_report = False
        return self.first_orders(scene) + self.report_turn(scene, text) + self.arrest_turn(scene, text)

    def first_orders(self, scene: Scene) -> list[str]:
        """The player enlisted earlier in this conversation. The prompt was written before that, so the superior
        knows of no duty and would make one up; on the player's next line, if the superior is here, the duty is
        given now (training step, incident or routine duty, by the usual rules) and the AI is told what it is."""
        service_id, self._first_orders = self._first_orders, None
        if service_id is None or scene.player_id is None:
            return []
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None or svc.service_id != service_id:
                return []
            boss = DU.superior_id(db, org, svc)
            if boss is None or boss not in scene.present:
                return []
            name = self._name(db, boss) or "the superior"
            lines = self._duty_lines(db, boss, name, org, svc, scene)
        if not lines:
            return []
        logger.log(23, f"Living World guard: first orders from {name} in the enlistment conversation")
        return [f"{scene.player_name} has just joined the guard, so here is what the guard's record says for "
                f"{name} now. These are {scene.player_name}'s real first orders: {name} gives exactly this, "
                f"and any other duty mentioned earlier in this conversation is replaced by it:\n"
                + "\n".join(f"- {line}" for line in lines)]

    def report_turn(self, scene: Scene, text: str) -> list[str]:
        """G2g: the player tells their superior (here) they are reporting back. The model often never begins its
        reply with ReportDuty, which left a drilled recruit stuck; so the rules judge the report now. A duty not done
        yet is not filed (no reprimand): the superior says what is missing. Returns notes for the AI, with the next
        orders when the report was accepted."""
        from livingworld.guard import training_words as TW
        if scene.player_id is None or not TW.enabled() or not TW.says_reporting(text):
            return []
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            boss = DU.superior_id(db, org, svc) if org else None
            if org is None or boss is None or boss not in scene.present:
                return []
            task = DU.in_hand(db, svc.member_id)
            d = DU.duty_for(db, task) if task else None
            if task is None or d is None or d.need == "quiz":
                return []
            name = self._name(db, boss) or "the superior"
            title = task.title
            if not DU.ready(db, task, d, scene.now):
                if DU.done_count(db, task, d) == 0:
                    missing = ("no fist bout yet: ask a fellow guard for one and fight it out" if d.need == "fight"
                               else f"nobody has seen {scene.player_name} at it yet")
                else:
                    missing = (f"not long enough yet: the shift runs until {P.clock(DU.shift_end(db, task, d))} and "
                               f"{scene.player_name} may report back from {P.clock(DU.assigned_gt(db, task) + d.shift_hours / 48.0)}")
                logger.log(23, f"Living World guard: report on duty {task.task_id} by word, not ready: {missing}")
                self._worded_report = True     # judged already: a ReportDuty now would file a false report
                return [P.report_note(name, scene.player_name, title, "not_ready", False, missing)]
        result, _ = self._duty_action(REPORT, boss, scene)
        self._worded_report = True     # a ReportDuty in the reply to this line would report the NEXT duty
        logger.log(23, f"Living World guard: report by word to {name}: {result}")
        r = self.filed
        if r is None or r.task is None or r.outcome != "done":
            return []
        notes = [P.report_note(name, scene.player_name, title, "done", r.trained, None)]
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            lines = self._duty_lines(db, boss, name, org, svc, scene) if org else []
        if lines:
            notes.append(f"Here is what the guard's record says for {name} now. Any next orders in it are "
                         f"{scene.player_name}'s real next orders: {name} gives exactly those and makes up no other:\n"
                         + "\n".join(f"- {line}" for line in lines))
        return notes

    def arrest_turn(self, scene: Scene, text: str) -> list[str]:
        """G2f: the player, serving, says someone present is under arrest. The rules decide what that person does;
        returns notes for the AI (added to the player's line)."""
        if scene.player_id is None or not AR.enabled() or not AR.wants_arrest(text):
            return []
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None or svc.status not in ("probation", "active"):
                return []
            names = {i: self._name(db, i) or "" for i in scene.present if i != scene.player_id and not is_child(db, i)}
            boss = DU.superior_id(db, org, svc)
            said = text.lower()
            named = [i for i, n in names.items() if n and (n.lower() in said or n.split()[0].lower() in said.split()
                                                           or n.split()[0].lower() + "," in said)]
            civilians = [i for i in names if i != boss and profiles.get(db, i) is None]
            target = named[0] if len(named) == 1 else civilians[0] if len(civilians) == 1 and not named else None
            if target is None or target == boss:
                return []
            guards = sum(1 for i in names if i != target and profiles.get(db, i) is not None)
            witnesses = [i for i in names if i != target]
            d = AR.decide(db, org, svc, target, names[target], scene.now, guards_present=guards,
                          location=scene.location, witnesses=witnesses)
            if d is None:
                return []
            a = d.arrest
            logger.log(23, f"Living World guard: arrest {a.event_id} of {a.name}: {a.response} (charge {a.charge}, "
                           f"evidence {len(d.evidence)}, p_submit {d.p_submit:.2f}, wrongful {a.wrongful})")
            jail = self._jail_name(org)
            self.notices.append(P.arrest_notice(a.name, d, jail))
            if a.response != AR.ARGUE:
                self.arrest_orders.append(ArrestOrder(a.name, a.response, a.event_id, a.days, org.jail))
            processor.process(db, {"appraisal": claims.appraise})
            return [P.arrest_note(a.name, scene.player_name, org, d, jail)]

    def _debug_suspect(self, db, org: O.Org, svc: S.Service | None, scene: Scene) -> str:
        if svc is None:
            return "not serving"
        others = [i for i in scene.present if i != scene.player_id and profiles.get(db, i) is None
                  and not is_child(db, i)]
        if len(others) != 1:
            return "talk to exactly one person who is not a guard to make them a suspect"
        name = self._name(db, others[0]) or ""
        inc = _suspect_incident(db, O.by_key(svc.org_key) or org, name, scene.now)
        return f"incident {inc.event_id}: {inc.title}; suspect {name}" if inc else "no incident names suspects"

    def take_arrest_orders(self) -> tuple[list[ArrestOrder], list[int]]:
        out, rel = self.arrest_orders, self.release_orders
        self.arrest_orders, self.release_orders = [], []
        return out, rel

    def _encounter_line(self, db, npc_id: int, name: str, p, svc: S.Service | None, scene: Scene) -> str | None:
        """G2e: the reason this NPC came over to the player on their shift, if they did just now."""
        if svc is None or not ENC.enabled():
            return None
        enc = ENC.fresh_for(db, npc_id, svc.member_id, scene.now)
        org = O.by_key(svc.org_key)
        if enc is None or org is None:
            return None
        self._encounters[npc_id] = enc
        return P.encounter_line(name, scene.player_name, org, svc, enc,
                                is_superior=npc_id == DU.superior_id(db, org, svc), is_guard=p is not None)

    def player_spoke(self, scene: Scene) -> int:
        """The player said something: an answer to whoever came over on shift (G2e), an answer in the training
        quiz (G2g), or part of their written report after a filed duty report."""
        if scene.player_id is None:
            return 0
        if scene.said:
            self._promotion_answer(scene)
            self._quiz_or_report(scene)
        if not self._encounters:
            return 0
        n = 0
        with self.store.transaction() as db:
            for npc_id, enc in list(self._encounters.items()):
                if npc_id in scene.present and ENC.answered(db, enc, scene.player_id, scene.now, scene.location):
                    n += 1
                    del self._encounters[npc_id]
        return n

    def _promotion_answer(self, scene: Scene) -> None:
        """G3: the player turns down the promotion their superior just gave them."""
        if self.promoted is None or not PR.declines(scene.said):
            return
        frm, to, boss = self.promoted
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None or svc.rank_key != to:
                return
            PR.decline(db, org, svc, frm, boss, scene.now)
        self.promoted = None
        self._declined = (frm, to)
        self.notices.append(f"Promotion turned down: you stay {org.a_title(frm)}.")

    def _quiz_or_report(self, scene: Scene) -> None:
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            boss = DU.superior_id(db, org, svc) if org else None
            if org is None or boss is None or boss not in scene.present:
                return
            if self.filed is not None and self.filed.task is not None and not TR.is_training(self.filed.task.template):
                self._report_lines.append(scene.said)
                return
            task = DU.in_hand(db, svc.member_id)
            d = DU.duty_for(db, task) if task else None
            if d is None or d.need != "quiz":
                return
            person = org.superior(svc.rank_key)
            result = TR.answer(db, org, task, scene.said, person.name if person else "", scene.now)
            logger.log(23, f"Living World guard: training quiz answer: {result}")
            witnesses = [i for i in scene.present if i != scene.player_id]
            if result == "passed":
                r = DU.report(db, org, svc, boss, scene.now, None, location=scene.location, witnesses=witnesses)
                self.notices.append("Training: law and orders quiz passed." +
                                    (" Training complete!" if r.trained else ""))
            elif result == "failed":
                T.change(db, task, "failed", "failed the training quiz", scene.now, witnesses=witnesses)
                self.notices.append("Training: quiz failed. Ask to try it again.")
            processor.process(db, {"appraisal": claims.appraise})

    def ended(self, scene: Scene) -> RP.Judged | None:
        """At the end of a conversation: a promotion given in it becomes news (G3), and what the player said after
        a filed report is kept as their written report."""
        if self.promoted is not None and scene.player_id is not None:
            frm, to, boss = self.promoted
            self.promoted = None
            with self.store.transaction() as db:
                svc = S.current(db, scene.player_id)
                org = O.by_key(svc.org_key) if svc else None
                if org is not None and svc.rank_key == to:
                    PR.announce(db, org, svc, frm, boss, scene.now, scene.location,
                                [i for i in scene.present if i != scene.player_id])
                    processor.process(db, {"appraisal": claims.appraise})
        lines, self._report_lines = self._report_lines, []
        if not lines or self.filed is None or self.filed.task is None or scene.player_id is None:
            return None
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None:
                return None
            j = RP.file(db, org, svc, self.filed.task, DU.superior_id(db, org, svc), " ".join(lines), scene.now,
                        scene.location, [i for i in scene.present if i != scene.player_id])
            if j is not None:
                logger.log(23, f"Living World guard: written report on duty {self.filed.task.task_id}: {j.verdict} "
                               f"({j.covered}/{j.items} covered, made up: {j.made_up})")
            return j

    def watch_plan(self, scene: Scene) -> ENC.Plan | None:
        """G2e: the watch windows still to come on the duty in hand, for the game (task 0 = none: clear)."""
        if scene.player_id is None or not ENC.enabled():
            return None
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            if svc is None:
                return None
            org = O.by_key(svc.org_key)
            task = DU.in_hand(db, svc.member_id) if org else None
            d = DU.duty_for(db, task) if task else None
            if task is None or not ENC.wants_windows(d):
                return ENC.Plan(0, 0.0, ())
            plan = ENC.plan(db, org, task, d, DU.assigned_gt(db, task))
            if self._watch_soon is not None and self._watch_soon[0] == task.task_id:
                plan = ENC.soon(db, org, task, self._watch_soon[1], plan.end)
            return plan

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
                held = DU.duty_for(db, task)
                if held is not None and held.training and held.need == "fight":
                    out.append(P.spar_line(name, player))
                    self.spar_partners.add(name)
            person = org.superior(svc.rank_key)
            if person is not None:
                out.append(P.report_to_line(name, player, person.name))
            return out
        if self.promoted is not None and self.promoted[1] == svc.rank_key:
            out.append(P.promoted_line(name, player, org, self.promoted[0], svc.rank_key))
        elif self._declined is not None:
            out.append(P.declined_line(name, player, org, *self._declined))
        elif PR.enabled() and svc.status in ("probation", "active"):
            out.append(P.board_line(name, player, PR.board(db, org, svc, scene.now)))
        if self.filed is not None and self.filed.task is not None:
            out.append(P.filed_line(name, player, self.filed, self.summary))
        if self.bookings:
            out.append(P.booked_line(name, player, self.bookings))
        held = AR.prisoners(db, org, scene.now) if AR.enabled() else []
        if held:
            out.append(P.prisoners_line(name, org, held, self._jail_name(org)))
        checks = DU.checks(db, org, svc, scene.now) if task is None else []
        if task is None and all(c.ok for c in checks):
            prev = DU.last(db, svc.member_id)
            incs = INC.for_member(db, org, svc, scene.now)
            step = TR.next_step(db, org, svc)
            d = (DU.training_duty(step) if step else DU.incident_duty(incs[0]) if incs
                 else DU.pick(org, svc, prev.template if prev else None, scene.now))
            if d is None:
                nxt = DU.next_start(org, svc, scene.now)
                out.append(P.no_duty_line(name, player, f"no duty for {org.a_title(svc.rank_key)} at this hour",
                                          nxt, scene.now))
            if d is not None:
                task = DU.assign(db, org, svc, npc_id, d, scene.now,
                                 witnesses=[i for i in scene.present if i != svc.member_id])
                self._assigned.add(task.task_id)
                boss_name = self._name(db, npc_id) or "your superior"
                if d.training:
                    n, total = TR.progress(db, org, svc)
                    how = {"fight": "Ask any Whiterun guard for a fist bout", "quiz": "Answer the questions now",
                           "escort": "Walk the round beside a senior guard"}.get(d.need, "Be there about "
                                                                                + P.hours_text(d.shift_hours / 48))
                    self.notices.append(f"Training {n + 1}/{total}: {d.title}. {how}"
                                        + ("." if d.need == "quiz" else f", then tell {boss_name} you're reporting back."))
                elif d.incident_id is not None:
                    self.notices.append(f"Trouble: {d.title}. " + ("Fight them there" if d.need == "fight" else
                                        "Go there") + f", then tell {boss_name} you're reporting back.")
                else:
                    self.notices.append(f"Duty: {d.title}. Shift: {P.hours_text(d.shift_hours / 24)}. "
                                        f"Be seen at your post, then tell {boss_name} you're reporting back.")
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
                done = DU.done_count(db, task, d)
                if d.training:
                    n, total = TR.progress(db, org, svc)
                    c = TR.course(org.key)
                    if d.need == "quiz":
                        right, wrong = TR.answers(db, task)
                        out.append(P.quiz_line(name, player, org, c.questions, right, wrong, n + 1, total, c.pass_right))
                    else:
                        out.append(P.training_line(name, player, d, n + 1, total, new=task.task_id in self._assigned,
                                                   seen=done > 0))
                else:
                    out.append(P.duty_line(name, player, task, d, DU.shift_end(db, task, d), scene.now,
                                           new=task.task_id in self._assigned, seen=done > 0))
                if task.task_id in self._assigned and ENC.enabled() and ENC.wants_windows(d):
                    out.append(P.watch_line(name, player))
        out.append(P.record_line(name, player, DU.record(db, svc)))
        past = RP.recent(db, svc)
        if past:
            out.append(P.reports_line(name, player, past))
        if org.pay:
            out.append(P.pay_line(name, player, DU.pay(db, org, svc, scene.now)))
        return out

    # ---- upkeep, once per conversation ----
    def refresh(self, scene: Scene) -> dict[str, list[int]]:
        """Missed duties, probation, and whether the player is at their post right now."""
        self._assigned = set()
        self.notices = []
        self._first_orders = None
        self.filed = None
        self.summary = None
        self._encounters = {}
        self._report_lines = []
        self.bookings = []
        self.promoted = None
        self._declined = None
        self.spar_partners = set()
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id) if scene.player_id is not None else None
            org = O.by_key(svc.org_key) if svc else None
            result = DU.upkeep(db, scene.now)
            if org is not None and svc.status in ("probation", "active"):
                result["incidents"] = [i.event_id for i in INC.spawn(db, org, scene.now, since=svc.joined_gt)]
                result["cold"] = INC.expire(db, org, scene.now)
            boss = DU.superior_id(db, org, svc) if org else None
            if AR.enabled():
                result["slipped"] = AR.slipped(db, scene.now)
                if org is not None and boss is not None and boss in scene.present:
                    self.bookings = AR.book(db, org, svc, boss, scene.now, scene.location,
                                            [i for i in scene.present if i != scene.player_id])
                    for b in self.bookings:
                        if b.wrongful:
                            self.release_orders.append(b.arrest.event_id)
                            self.notices.append(f"Booked: {b.arrest.name} let out. Wrongful arrest: reprimanded.")
                        else:
                            self.notices.append(f"Booked: {b.arrest.name} for {b.arrest.charge}.")
                    result["booked"] = [b.arrest.event_id for b in self.bookings]
                    svc = S.get(db, svc.service_id)
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
            if d is not None and boss is not None and boss in scene.present and DU.ready(db, held, d, scene.now):
                # coming back to the superior when the duty is done files the report: no action needed
                witnesses = [i for i in scene.present if i != scene.player_id]
                self.filed = DU.report(db, org, svc, boss, scene.now, scene.said, location=scene.location,
                                       witnesses=witnesses)
                result["reported"] = [held.task_id]
                if self.filed.outcome == "done" and ENC.enabled() and ENC.wants_windows(d):
                    self.summary = ENC.file_summary(db, org, S.get(db, svc.service_id), self.filed.task, boss,
                                                    scene.now, scene.location, witnesses)
                self.notices.append(P.filed_notice(self._name(db, boss) or "your superior", self.filed, d,
                                                   self.summary))
                if self.filed.trained:
                    self.notices.append("Training complete!")
                d = None
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
            if org is not None and svc is not None and PR.enabled():
                self._promotion_board(db, org, S.get(db, svc.service_id), boss, scene, result)
            processor.process(db, {"appraisal": claims.appraise})
        return result

    def _promotion_board(self, db, org: O.Org, svc: S.Service, boss: int | None, scene: Scene,
                         result: dict[str, list[int]]) -> None:
        """G3: run the board; with the superior here, a promotion that is due takes effect now."""
        offered = PR.run(db, org, svc, scene.now)
        if offered:
            result["promotion_due"] = [svc.service_id]
        if boss is not None and boss in scene.present:
            took = PR.take(db, org, svc, boss, scene.now)
            if took:
                self.promoted = (took[0], took[1], boss)
                result["promoted"] = [svc.service_id]
                self.notices.append(f"Promoted: {self._name(db, boss) or 'your superior'} names you "
                                    f"{org.title(took[1])} of {org.name}. (Say you decline to turn it down.)")
                return
        if self.filed is not None and self.filed.outcome == "done":
            self.notices.append(PR.short(PR.board(db, org, S.get(db, svc.service_id), scene.now)))

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
            self._first_orders = svc.service_id
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
                if self._worded_report:
                    return "already judged on the player's words this turn", None
                held = DU.in_hand(db, svc.member_id)
                d = DU.duty_for(db, held) if held is not None else None
                r = DU.report(db, org, svc, npc_id, scene.now, scene.said, location=scene.location,
                              witnesses=witnesses)
                if r.outcome == "done" and r.task is not None:
                    self.filed = r            # what the player says next is their written report
                if r.outcome == "done" and ENC.enabled() and ENC.wants_windows(d):
                    sm = ENC.file_summary(db, org, S.get(db, svc.service_id), r.task, npc_id, scene.now,
                                          scene.location, witnesses)
                    self.notices.append(f"Shift report: {sm.counts()}.")
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

    def chest_due(self, scene: Scene) -> O.Item | None:
        """The footlocker the game should place by the member's bunk: once per service, while serving."""
        if scene.player_id is None:
            return None
        with self.store.transaction() as db:
            svc = S.current(db, scene.player_id)
            org = O.by_key(svc.org_key) if svc else None
            if org is None or org.chest is None or org.bed is None or svc.status not in ("probation", "active"):
                return None
            given = S.last_event_gt(db, "chest_granted", scene.player_id, org.key)
            if given is not None and given >= svc.joined_gt:
                return None
            S.event(db, "chest_granted", [scene.player_id], {"org": org.key}, scene.now, None, [])
            return org.chest

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
                if boss is None or boss in scene.present:
                    return []
                if PR.enabled() and PR.pending(db, svc) is not None:
                    return [boss]                         # G3: news of a promotion
                if not all(c.ok for c in DU.checks(db, org, svc, scene.now)):
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
            if cmd.verb == "watch":
                held = DU.in_hand(db, svc.member_id) if svc is not None else None
                d = DU.duty_for(db, held) if held is not None else None
                if not ENC.wants_windows(d):
                    return "no routine duty in hand (watch windows come with routine duties)"
                self._watch_soon = (held.task_id, scene.now)
                return f"watch windows of duty {held.task_id} brought forward (sent with the next line)"
            if cmd.verb == "suspect":
                return self._debug_suspect(db, org, svc, scene)
            if cmd.verb == "board" and svc is not None and O.by_key(svc.org_key) is not None:
                self.notices.append(PR.short(PR.board(db, O.by_key(svc.org_key), svc, scene.now)))
            return run(db, cmd, org, scene.player_id, scene.now, scene.location)


def _suspect_incident(db, org: O.Org, name: str, now: float) -> INC.Incident | None:
    """Open a theft incident that names ``name`` as the suspect (test command)."""
    k = next((k for k in INC.kinds(org.key) if k.suspects), None)
    if k is None:
        return None
    seed = INC._roll(org.key, now, k.key, name)
    inc = INC._open(db, org, k, int(now), now, seed)
    payload = json.loads(db.execute("SELECT payload_json FROM world_event WHERE event_id = ?", (inc.event_id,)).fetchone()[0])
    if payload.get("suspect") == name:
        return inc
    INC.close(db, inc, "went cold", now)
    payload.update({"suspect": name, "culprit": name, "why": payload["why"].split(". Word is")[0] + f". Word is it was {name}"})
    event_id = S.event(db, "incident", [], payload, now, inc.where[0] if inc.where else org.hold, [], "admin")
    return INC.get(db, event_id)


def run(db, cmd: D.Command, org: O.Org, player_id: int, now: float, location: str | None) -> str:
    """Apply one test command (shared by the in-game debug line and the developer CLI)."""
    svc = S.current(db, player_id)
    if cmd.verb == "status":
        if svc is None:
            return "not serving"
        o = O.by_key(svc.org_key)
        return (f"{o.name if o else svc.org_key}: {o.title(svc.rank_key) if o else svc.rank_key}, {svc.status}, "
                f"joined day {svc.joined_gt:.2f} ({svc.path}), merit {svc.merit:g}")
    if cmd.verb in ("board", "promote"):
        o = O.by_key(svc.org_key) if svc else None
        if svc is None or o is None:
            return "not serving"
        if cmd.verb == "board":
            return " | ".join(PR.explain(PR.board(db, o, svc, now)))
        target = o.next_rank(svc.rank_key)
        if target is None:
            return f"already {o.a_title(svc.rank_key)}, the highest rank"
        if PR.pending(db, svc) is not None:
            return f"a promotion to {o.title(PR.pending(db, svc))} is already due: talk to your superior"
        PR.offer(db, o, svc, target, now, source="admin")
        return f"promotion to {target.title} due: your superior announces it when you next talk"
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
    if cmd.verb == "training":
        if TR.trained(db, svc):
            return "training already completed"
        S.change(db, svc, "developer: training completed", now, kind="trained", source="admin")
        return "recruit training counted as completed"
    if cmd.verb == "duty":
        if held is not None:
            return f"already on duty: {held.title}"
        prev = DU.last(db, svc.member_id)
        step = TR.next_step(db, org, svc)
        d = DU.training_duty(step) if step else DU.pick(org, svc, prev.template if prev else None, now, any_hour=True)
        if d is None:
            return "no duty fits this rank"
        task = DU.assign(db, org, svc, boss, d, now, source="admin")
        return f"duty {task.task_id}: {d.title} ({d.shift_hours:g} h, report to {person.name if person else 'superior'})"
    if held is None:
        return "no duty in hand"
    if cmd.verb == "seen":
        d = DU.duty_for(db, held)
        if d is not None and d.need == "quiz":
            c = TR.course(org.key)
            for q in c.questions[:c.pass_right]:
                T.change(db, held, "in_progress", f"quiz answer: right: {q.key}", now, source="admin")
            return f"quiz answers counted right for: {held.title}"
        if d is not None and d.need == "fight":
            DU.mark_fought(db, svc.member_id, d.where[0] if d.where else location, now)
            return f"fought at the place of: {held.title}"
        DU.mark_seen(db, svc.member_id, location, now, force=True)
        return f"seen at the post of: {held.title}"
    r = DU.report(db, org, svc, boss, now, "(developer report)", location=location, source="admin")
    return f"report: {r.outcome}" + (" (commended)" if r.commended else "")
