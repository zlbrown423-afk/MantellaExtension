"""What the integration calls: note who is a guard, add guard lines to prompts, react to EnlistRecruit,
list leaders who would invite the player, and run the in-conversation test commands. Every method opens
its own transaction."""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.guard import debug as D, orgs as O, prompts as P, profiles, recruit as R, store as S
from livingworld.guard.profiles import GuardFacts
from livingworld.social import claims, processor
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore

ENLIST = "mantella_lw_guard_enlist"
ACTIONS = (ENLIST,)
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


@dataclass
class Issue:
    """Items for the game to hand the player (reply key mantella_lw_guard_issue)."""
    items: list[O.Item]

    def key_value(self) -> str:
        return ";".join(f"{i.name}|{i.plugin}|{i.form_id}" for i in self.items if i.form_id)

    def form_ids(self) -> list[int]:
        return [int(i.form_id, 16) for i in self.items if i.form_id]

    def plugins(self) -> list[str]:
        return [i.plugin for i in self.items if i.form_id]


class Guard:
    def __init__(self, store: WorldStore) -> None:
        self.store = store

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

    def leader_for(self, npc_id: int, scene: Scene) -> int:
        """``npc_id`` if they can take people on; else the only leader present who can (the action means them)."""
        with self.store.transaction() as db:
            p = profiles.get(db, npc_id)
            if p is not None and p.recruits:
                return npc_id
            able = [i for i in scene.present if (q := profiles.get(db, i)) is not None and q.recruits]
            return able[0] if len(able) == 1 else npc_id

    # ---- actions ----
    def action(self, identifier: str, npc_id: int, scene: Scene) -> tuple[str, Issue | None]:
        """Handle a guard action by ``npc_id`` (the leader). Returns (what happened, items to hand out)."""
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

    # ---- the game's approach step ----
    def wants_a_word(self, scene: Scene) -> list[int]:
        """Leaders met before (not in this conversation) who would invite the player to join, and have not
        in the last week: the game may have one come over (the same approach list as tasks)."""
        if scene.player_id is None:
            return []
        out = []
        with self.store.transaction() as db:
            if S.current(db, scene.player_id) is not None:
                return []
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
    return "nothing done"
