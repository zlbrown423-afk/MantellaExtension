"""Barracks banter: a few guards are bullies who heckle the player on duty, and a few are gossips.

Each generic guard gets a temperament once, the first time it is needed, and keeps it for good (stored in
``guard_profile.traits_json`` like their name, so it is not on the save timeline: a person's nature does not
roll back). It is picked from a hash of who they are, so the same guard is always the same:

* **bully** (about 1 in 12): rides new blood. When the player is on duty and starts a conversation with this
  guard alone, now and then the guard heckles them: a test of nerve in words only. A bully never heckles
  when the conversation was already going (the guard joined it later, or the player started it with someone
  else), and not when the player outranks a plain guardsman.
* **gossip** (about 1 in 7): loves barracks talk. Passes on what is said about the player in the guard (an
  enlistment, a reprimand, an arrest, a bully's heckling) once each, warns a member about the bullies, and
  trades town rumours from what they already know.
* everyone else: plain.

On duty means the player serves (probation or active) and either has a routine duty or a training step in
hand, or is wearing their own guard's uniform. Trouble comes first: no heckling while the duty in hand is an
incident or while the player is walking a prisoner to the cells; and a bully's prompt tells
them to drop the act at once if the player brings up real trouble or an order. Heckling never starts a
conversation on its own, never stops the player from leaving and never changes the service record, so it
cannot get in the way of watching for crime on shift.

Rarity, so it stays a surprise: a given bully heckles at most once every ``BULLY_GAP_DAYS`` game days, all
bullies of one guard together at most once every ``ORG_GAP_DAYS``, and an eligible conversation brings it
on only ``HECKLE_CHANCE`` of the time.

Rules only, no extra LLM call, no new table. Follows ``MANTELLA_LW_GUARD``; off on its own with
``MANTELLA_LW_GUARD_BANTER=0``.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from livingworld.guard import orgs as O, profiles, store as S

ENV = "MANTELLA_LW_GUARD_BANTER"
BULLY, GOSSIP, PLAIN = "bully", "gossip", "plain"
BULLY_SHARE = 0.08            # of generic guards, by hash ...
BULLY_FLOOR_EVERY = 12        # ... and at least one in every twelve met, so a hold always has one or two
GOSSIP_SHARE = 0.14
BULLY_GAP_DAYS = 4.0          # one bully heckles the player at most this often
ORG_GAP_DAYS = 1.5            # all bullies of one guard together
HECKLE_CHANCE = 0.5           # an eligible conversation brings it on this often
GOSSIP_DAYS = 10.0            # guard news this recent is worth passing on
GOSSIP_ITEMS = 2              # pieces of news per conversation


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _roll(*parts) -> float:
    h = int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)
    return (h % 100_000) / 100_000


def rolled(org_key: str, plugin: str, ref_or_base: str) -> str:
    """The temperament a guard's identity hashes to (before the one-in-twelve floor for bullies)."""
    x = _roll(org_key, "temperament", plugin, ref_or_base)
    if x < BULLY_SHARE:
        return BULLY
    if x < BULLY_SHARE + GOSSIP_SHARE:
        return GOSSIP
    return PLAIN


def _traits(raw: str | None) -> dict:
    try:
        out = json.loads(raw or "{}")
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}


def temperament(db, npc_id: int) -> str:
    """This guard's temperament: picked and stored the first time it is asked for. Leaders and named guards
    (no given name) are always plain."""
    row = db.execute("SELECT p.org_key, p.given_name, p.traits_json, n.plugin, n.base_form_id, n.ref_form_id"
                     " FROM guard_profile p JOIN npc n ON n.npc_id = p.npc_id WHERE p.npc_id = ? AND n.is_child = 0",
                     (npc_id,)).fetchone()
    if row is None:
        return PLAIN
    traits = _traits(row["traits_json"])
    if traits.get("temperament") in (BULLY, GOSSIP, PLAIN):
        return traits["temperament"]
    if not row["given_name"] or O.leader_of(row["plugin"], row["base_form_id"]) is not None:
        return PLAIN               # not stored: a leader stays plain, and a named guard may be given a name later
    kind = rolled(row["org_key"], row["plugin"], row["ref_form_id"] or row["base_form_id"])
    if kind != BULLY:
        counts = {k: n for k, n in db.execute(
            "SELECT json_extract(traits_json, '$.temperament'), COUNT(*) FROM guard_profile WHERE org_key = ?"
            " AND json_extract(traits_json, '$.temperament') IS NOT NULL GROUP BY 1", (row["org_key"],))}
        met = sum(counts.values()) + 1
        if counts.get(BULLY, 0) < met // BULLY_FLOOR_EVERY:
            kind = BULLY
    traits["temperament"] = kind
    db.execute("UPDATE guard_profile SET traits_json = ? WHERE npc_id = ?", (json.dumps(traits), npc_id))
    return kind


def _name(db, npc_id: int) -> str:
    p = profiles.get(db, npc_id)
    if p is not None and p.given_name:
        return p.given_name
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else "a guard"


# ---- on duty ----

@dataclass(frozen=True)
class Duty:
    on: bool
    why: str            # 'duty' | 'uniform' | what keeps it off


def on_duty(db, org: O.Org, svc: S.Service | None, uniform_org: str | None, now: float) -> Duty:
    from livingworld.guard import arrest as AR, duty as DU
    if svc is None or svc.org_key != org.key or svc.status not in ("probation", "active"):
        return Duty(False, "not serving")
    task = DU.in_hand(db, svc.member_id)
    d = DU.duty_for(db, task) if task is not None else None
    if d is not None and d.incident_id is not None:
        return Duty(False, "trouble in hand")
    if AR.enabled() and any(AR.status(db, a, now) == "escorting" for a in AR.by_member(db, svc.member_id)):
        return Duty(False, "escorting a prisoner")
    if d is not None:
        return Duty(True, "duty")
    if uniform_org == org.key:
        return Duty(True, "uniform")
    return Duty(False, "off duty")


# ---- heckling ----

def heckles(db, member_id: int, bully_id: int | None = None) -> list[dict]:
    sql = ("SELECT event_id, actors_json, game_time FROM v_visible_event WHERE type = 'guard.heckle' AND"
           " json_extract(actors_json, '$[0]') = ?")
    params: list = [member_id]
    if bully_id is not None:
        sql += " AND json_extract(actors_json, '$[1]') = ?"
        params.append(bully_id)
    return [dict(r) for r in db.execute(sql + " ORDER BY game_time, event_id", tuple(params))]


def pick_heckler(db, svc: S.Service | None, opening: set[int], player_id: int, now: float, location: str | None,
                 uniform_org: str | None, *, force: bool = False) -> tuple[int | None, str]:
    """Whether this conversation brings a heckle: (the bully, why). Called once, when it starts. ``opening``
    is everyone in it at the start: only the player and one guard. ``force`` (test command) skips the
    temperament, rank, gap and chance checks, but never the duty check."""
    others = [i for i in opening if i != player_id]
    if svc is None or len(others) != 1:
        return None, "not one-on-one"
    npc_id = others[0]
    p = profiles.get(db, npc_id)
    org = O.by_key(svc.org_key)
    if p is None or org is None or p.org_key != org.key or p.is_leader:
        return None, "not a fellow guard"
    if not force and temperament(db, npc_id) != BULLY:
        return None, "not a bully"
    duty = on_duty(db, org, svc, uniform_org, now)
    if not duty.on:
        return None, duty.why
    if not force:
        if org.rank_index(svc.rank_key) > org.rank_index(org.generic_rank):
            return None, "the player outranks them"
        mine = heckles(db, svc.member_id, npc_id)
        if mine and now - mine[-1]["game_time"] < BULLY_GAP_DAYS:
            return None, "heckled lately"
        anyone = heckles(db, svc.member_id)
        if anyone and now - anyone[-1]["game_time"] < ORG_GAP_DAYS:
            return None, "someone heckled lately"
        if _roll(org.key, "heckle", npc_id, int(now * 8)) >= HECKLE_CHANCE:
            return None, "not this time"
    S.event(db, "heckle", [svc.member_id, npc_id], {"org": org.key, "service_id": svc.service_id, "via": duty.why},
            now, location, [])
    return npc_id, duty.why


def _ammo(db, org: O.Org, svc: S.Service, now: float) -> list[str]:
    """What a bully can needle the player about, from the record."""
    from livingworld.guard import duty as DU, training as TR
    out = []
    days = now - svc.joined_gt
    rank = org.title(svc.rank_key)
    if days < 3:
        out.append(f"being a green {rank} only {max(1, int(days + 0.5))} day{'s' if days >= 1.5 else ''} in")
    else:
        out.append(f"still being a mere {rank}")
    if svc.rank_key == (org.ranks[0].key if org.ranks else None) and not TR.trained(db, svc):
        out.append("not even through recruit training yet")
    rec = DU.record(db, svc)
    if rec.reprimanded:
        last = db.execute("SELECT json_extract(payload_json, '$.why') FROM v_visible_event WHERE type ="
                          " 'guard.reprimanded' AND json_extract(actors_json, '$[0]') = ? ORDER BY game_time DESC"
                          " LIMIT 1", (svc.member_id,)).fetchone()
        out.append("the reprimand they got" + (f" ({last[0]})" if last and last[0] else ""))
    if rec.early:
        out.append("leaving a post early")
    if rec.false_reports:
        out.append("a report that did not hold up")
    if rec.commended:
        out.append("the commendation, as if it made them the Jarl's favourite")
    out.append("how the uniform sits on them, and whether they would last a night on the wall")
    return out


def heckle_lines(db, name: str, player: str, org: O.Org, svc: S.Service, npc_id: int, now: float,
                 via: str) -> list[str]:
    before = [h for h in heckles(db, svc.member_id, npc_id) if now - h["game_time"] > 0.02]
    where = "on duty" if via == "duty" else f"in the uniform of {org.name}"
    out = [
        f"{name} is one of the hard cases of {org.name}: likes to ride new blood and see who cracks. Right now "
        f"{name} heckles {player}, who is {where}: needles them about "
        + "; ".join(_ammo(db, org, svc, now)[:3]) + ". Sly, mocking and persistent, a test of nerve in words only.",
        f"{name} never lays a hand on {player}, makes no real threat, accuses them of no crime, and does not stop them "
        f"going back to their post. A few jabs, not a speech.",
        f"How {player} takes it matters to {name}: a cool head, a sharp comeback or calmly getting on with the job "
        f"earns grudging respect and {name} lets it go; losing their temper, whining or threatening makes {name} push "
        f"harder and laugh about it later. Running to a superior makes {name} call them soft.",
        f"The watch comes first: if {player} brings up real trouble (a crime, a fight, someone hurt) or an order from "
        f"a superior, {name} drops the act at once and does the job.",
    ]
    if before:
        out.append(f"{name} has given {player} a hard time before ({len(before)} time{'s' if len(before) != 1 else ''}).")
    return out


def bully_aside(name: str, player: str) -> str:
    """A bully who is not heckling right now (off duty, or not this time)."""
    return (f"{name} has a mean streak toward newer guards and enjoys taking them down a peg, but is not looking for "
            f"it with {player} right now.")


# ---- gossip ----

_NEWS = {
    "guard.enlisted": "{player} joined {guard} as a recruit",
    "guard.trained": "{player} got through recruit training",
    "guard.promoted": "{player} was made {rank}",
    "guard.commended": "{player} was commended by their superior",
    "guard.reprimanded": "{player} got a reprimand ({why})",
    "guard.arrested": "{player} brought in {culprit}",
    "guard.cleared": "{player} dealt with {title}",
    "guard.booked": "{player} locked someone up who had done nothing, and caught it from the superior",
    "guard.dismissed": "{player} was thrown out of {guard}",
    "guard.heckle": "{bully} gave {player} a hard time on duty",
}


def _news_text(db, row, org: O.Org, player: str, listener: int) -> str | None:
    p = _traits(row["payload_json"])
    actors = json.loads(row["actors_json"])
    kind = row["type"]
    if kind == "guard.booked" and not p.get("wrongful"):
        return None
    fill = {"player": player, "guard": org.name, "rank": org.title(p.get("rank")) if p.get("rank") else "something",
            "why": p.get("why") or "something", "culprit": p.get("culprit") or "someone",
            "title": (p.get("title") or "some trouble").lower(), "bully": ""}
    if kind == "guard.heckle":
        bully = actors[1] if len(actors) > 1 else None
        if bully is None or bully == listener:
            return None
        fill["bully"] = _name(db, bully)
    return _NEWS[kind].format(**fill)


def untold_news(db, org: O.Org, member_id: int, listener: int, player: str, now: float) -> list[tuple[int, str]]:
    """Guard news about the player this gossip has not passed on yet (most recent first)."""
    told: set[int] = set()
    for r in db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'guard.gossip_told' AND"
                        " json_extract(actors_json, '$[1]') = ?", (listener,)):
        told.update(int(x) for x in _traits(r[0]).get("told", ()))
    kinds = tuple(_NEWS)
    marks = ",".join("?" * len(kinds))
    rows = db.execute(f"SELECT event_id, type, actors_json, payload_json, game_time FROM v_visible_event WHERE type IN"
                      f" ({marks}) AND json_extract(actors_json, '$[0]') = ? AND json_extract(payload_json, '$.org') = ?"
                      f" AND game_time >= ? AND game_time <= ? ORDER BY game_time DESC, event_id DESC",
                      (*kinds, member_id, org.key, now - GOSSIP_DAYS, now + 1e-6)).fetchall()
    out = []
    for r in rows:
        if r["event_id"] in told:
            continue
        # the gossip does not "hear" what they took part in themselves as news
        if listener in json.loads(r["actors_json"]) and r["type"] != "guard.heckle":
            continue
        text = _news_text(db, r, org, player, listener)
        if text:
            out.append((r["event_id"], text))
    return out


def bullies(db, org: O.Org, besides: int) -> list[str]:
    """The bullies of this guard the player has met (by name), other than ``besides``."""
    out = []
    for p in profiles.roster(db, org.key):
        if p.npc_id != besides and p.given_name and temperament(db, p.npc_id) == BULLY:
            out.append(p.given_name)
    return out


def gossip_lines(db, name: str, player: str, org: O.Org, svc: S.Service | None, npc_id: int, player_id: int,
                 now: float, location: str | None) -> list[str]:
    out = [f"{name} is the worst gossip in {org.name}: loves barracks talk and town rumours, passes on whatever they "
           f"have heard (getting a detail or two a little wrong), and always wants to know what {player} has heard."]
    member = svc is not None and svc.org_key == org.key
    if member:
        news = untold_news(db, org, svc.member_id, npc_id, player, now)[:GOSSIP_ITEMS]
        if news:
            out.append(f"What {name} has heard about {player} in the barracks and is itching to bring up: "
                       + "; ".join(t for _, t in news) + ".")
            S.event(db, "gossip_told", [player_id, npc_id], {"org": org.key, "told": [i for i, _ in news]}, now,
                    location, [])
        mean = bullies(db, org, npc_id)
        if mean:
            out.append(f"{name} may warn {player} about {', '.join(mean[:2])}: "
                       f"{'they ride' if len(mean) > 1 else 'rides'} the newer guards hard.")
    return out


# ---- test commands ("debug guard bully|gossip"): kept in the world, so they survive a server restart ----

def force(db, player_id: int, verb: str, now: float) -> None:
    S.event(db, "banter_test", [player_id], {"verb": verb, "used": False}, now, None, [])


def forced(db, player_id: int) -> tuple[int, str] | None:
    """The test command waiting for the next one-on-one talk: (event id, verb), or None."""
    row = db.execute("SELECT e.event_id, json_extract(e.payload_json, '$.verb') FROM v_visible_event e WHERE e.type ="
                     " 'guard.banter_test' AND json_extract(e.actors_json, '$[0]') = ? AND NOT EXISTS (SELECT 1 FROM"
                     " v_visible_event u WHERE u.type = 'guard.banter_test_used' AND json_extract(u.payload_json,"
                     " '$.test') = e.event_id) ORDER BY e.event_id DESC LIMIT 1", (player_id,)).fetchone()
    return (row[0], row[1]) if row else None


def use_force(db, player_id: int, test_id: int, now: float) -> None:
    S.event(db, "banter_test_used", [player_id], {"test": test_id}, now, None, [])
