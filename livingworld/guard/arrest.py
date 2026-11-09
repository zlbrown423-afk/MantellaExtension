"""G2f: arrests by the player while they serve in a hold's guard.

The player says so in a conversation ("Brenuin, you're under arrest"). Rules, not the AI, decide what happens;
the AI only plays it out:

- **The charge needs evidence.** Evidence is what the record holds against that person: being named as the
  suspect of an incident the member was sent to (stronger when the member asked around at the place), or a
  theft or assault of theirs the game reported in the last ``evidence_days`` (stronger when the member saw it).
- **No evidence:** the person argues and asks on what charge (``argue``). Saying it again in the same
  conversation arrests them anyway, as a *wrongful* arrest: they go, protesting, and the superior lets them out
  and reprimands the member when the member next reports in.
- **With evidence** a seeded roll decides: they go quietly (``submit``), run (``flee``) or fight (``fight``).
  Strong evidence, other guards standing by, a higher rank and having run or fought already (caught again)
  make going quietly likelier; fighting folk (sellswords, bandits, Orcs...) fight more often than they run.
- **Escort and cell:** a person who goes quietly follows the member; the game locks them in the hold's jail
  (the vanilla prisoner marker, inside the cell) once the member brings them there, and lets them out when
  the sentence is served (``guard.jailed`` / ``guard.released``, reported by the game as ``jail`` records).
  Not brought in within ``escort_days``: they slip away.
- **Booking:** the next time the member talks to their superior, each prisoner they brought in is booked:
  merit (more for an incident's suspect), and the news gets around (``guard.arrested``). A wrongful arrest is
  booked as a reprimand instead, and the prisoner is let out at once.

Everything is a ``guard.*`` world event, so it rolls back with saves. A shared court (civic C3) can later turn
a booking into a case.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from importlib import resources

from livingworld.guard import orgs as O, store as S

ENV = "MANTELLA_LW_GUARD_ARRESTS"
SUBMIT, ARGUE, FLEE, FIGHT = "submit", "argue", "flee", "fight"
RESPONSES = (SUBMIT, ARGUE, FLEE, FIGHT)
JAILED, RELEASED = "jailed", "released"

ARREST_WORDS = re.compile(
    r"\b(?:under arrest|i(?:'m| am) arresting|i arrest you|arrest(?:ing)? (?:you|him|her|them)|come quietly|"
    r"you(?:'re| are) coming with me to the (?:cells|jail|dungeon))\b", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    submit_base: float = 0.45
    submit_per_evidence: float = 0.35
    submit_guards: float = 0.15
    submit_per_rank: float = 0.04
    submit_caught: float = 0.35
    submit_wrongful: float = 0.85
    tough_penalty: float = 0.2
    fight_share: float = 0.35
    fight_share_tough: float = 0.65
    tough_words: tuple[str, ...] = ("bandit", "sellsword", "mercenary", "warrior", "orc", "berserker", "uthgerd")
    evidence_days: float = 10.0
    escort_days: float = 1.0
    caught_days: float = 2.0
    insist_days: float = 0.05
    merit_booked: float = 1.0
    merit_suspect: float = 0.75
    merit_wrongful: float = -1.5
    sentences: dict = field(default_factory=lambda: {"theft": 1.0, "brawling": 1.0, "assault": 2.0})
    default_days: float = 1.0


@functools.lru_cache(maxsize=None)
def config(org_key: str) -> Config:
    org = O.by_key(org_key)
    if org is None or not org.duty_file:
        return Config()
    raw = json.loads(resources.files("livingworld.guard").joinpath("data", org.duty_file).read_text(encoding="utf-8"))
    a = dict(raw.get("arrest") or {})
    if "tough_words" in a:
        a["tough_words"] = tuple(a["tough_words"])
    a.pop("_note", None)
    return Config(**a)


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def wants_arrest(text: str | None) -> bool:
    return bool(text and ARREST_WORDS.search(text))


@dataclass(frozen=True)
class Evidence:
    charge: str          # 'theft' | 'brawling' | 'assault'
    why: str             # "named as the suspect for the purse snatched in the market"
    strength: float      # 0..1
    incident: int | None = None


@dataclass
class Arrest:
    event_id: int
    org_key: str
    service_id: int
    member_id: int
    npc_id: int
    name: str
    charge: str | None
    evidence: list[str]
    wrongful: bool
    response: str
    incident: int | None
    days: float
    gt: float
    location: str | None

    @property
    def mode(self) -> str:
        return self.response


def _row(row) -> Arrest:
    p = json.loads(row["payload_json"])
    a = json.loads(row["actors_json"])
    return Arrest(row["event_id"], p["org"], p["service_id"], a[0], p["npc"], p.get("name", ""), p.get("charge"),
                  list(p.get("evidence") or ()), bool(p.get("wrongful")), p["response"], p.get("incident"),
                  float(p.get("days", 1.0)), row["game_time"], row["location"])


def get(db, event_id: int) -> Arrest | None:
    row = db.execute("SELECT * FROM v_visible_event WHERE event_id = ? AND type = 'guard.arrest'", (event_id,)).fetchone()
    return _row(row) if row else None


def _arrests(db, where: str, params: tuple) -> list[Arrest]:
    rows = db.execute(f"SELECT * FROM v_visible_event WHERE type = 'guard.arrest' AND {where}"
                      " ORDER BY game_time, event_id", params).fetchall()
    return [_row(r) for r in rows]


def of_npc(db, npc_id: int) -> list[Arrest]:
    return _arrests(db, "json_extract(payload_json, '$.npc') = ?", (npc_id,))


def by_member(db, member_id: int) -> list[Arrest]:
    return _arrests(db, "json_extract(actors_json, '$[0]') = ?", (member_id,))


def _after(db, kind: str, arrest_id: int) -> dict | None:
    row = db.execute(f"SELECT payload_json, game_time FROM v_visible_event WHERE type = 'guard.{kind}' AND"
                     " json_extract(payload_json, '$.arrest') = ? ORDER BY game_time DESC, event_id DESC LIMIT 1",
                     (arrest_id,)).fetchone()
    if row is None:
        return None
    out = json.loads(row[0])
    out["gt"] = row[1]
    return out


def jailed(db, a: Arrest) -> dict | None:
    return _after(db, JAILED, a.event_id)


def released(db, a: Arrest) -> dict | None:
    return _after(db, RELEASED, a.event_id)


def booked(db, a: Arrest) -> dict | None:
    return _after(db, "booked", a.event_id)


def status(db, a: Arrest, now: float) -> str:
    """'argued' | 'resisted' | 'escorting' | 'jailed' | 'released' | 'slipped away'."""
    if a.response == ARGUE:
        return "argued"
    if a.response in (FLEE, FIGHT):
        return "resisted"
    r = released(db, a)
    if r is not None:
        return "slipped away" if r.get("why") == "escaped" else "released"
    j = jailed(db, a)
    if j is not None:
        return "released" if now >= float(j["until"]) else "jailed"
    return "escorting"


def held(db, npc_id: int, now: float) -> Arrest | None:
    """The arrest that has ``npc_id`` in the member's custody or in a cell now, if any."""
    for a in reversed(of_npc(db, npc_id)):
        if status(db, a, now) in ("escorting", "jailed"):
            return a
    return None


# ---- evidence ----

def evidence(db, org: O.Org, svc: S.Service, npc_id: int, name: str, now: float) -> list[Evidence]:
    from livingworld.guard import duty as DU, incident as INC
    cfg = config(org.key)
    out: list[Evidence] = []
    first = (name or "").lower()
    for inc in INC.open_incidents(db, org, now):
        if not inc.suspect or inc.suspect.lower() != first:
            continue
        task = DU.in_hand(db, svc.member_id)
        mine = task is not None and INC.event_id_of(task.template) == inc.event_id
        asked = mine and DU.seen_count(db, task) > 0
        why = f"named as the suspect: {inc.why}"
        if asked:
            why += f", and {DU._name(db, svc.member_id)} asked around {inc.place}"
        out.append(Evidence(inc.charge or "theft", why, 0.8 if asked else 0.5, inc.event_id))
    rows = db.execute(
        "SELECT event_id, type, payload_json, location, game_time FROM v_visible_event WHERE type IN ('theft','violence')"
        " AND json_extract(actors_json, '$[0]') = ? AND game_time >= ? ORDER BY game_time DESC",
        (npc_id, now - cfg.evidence_days)).fetchall()
    for r in rows:
        p = json.loads(r["payload_json"])
        if r["type"] == "violence" and p.get("via") not in (None, "assault"):
            continue
        saw = db.execute("SELECT 1 FROM event_witness WHERE event_id = ? AND npc_id = ?",
                         (r["event_id"], svc.member_id)).fetchone() is not None
        charge = "theft" if r["type"] == "theft" else "assault"
        what = f"stole {p.get('item') or 'something'}" if charge == "theft" else "attacked someone"
        out.append(Evidence(charge, f"{what} at {r['location'] or 'a place nearby'}"
                            + (f", seen by {DU._name(db, svc.member_id)}" if saw else ""), 0.9 if saw else 0.6))
    return out


def _tough(db, npc_id: int, name: str, cfg: Config) -> bool:
    row = db.execute("SELECT race FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    words = f"{name} {row[0] if row and row[0] else ''}".lower()
    return any(w in words for w in cfg.tough_words)


def _roll(*parts) -> float:
    return (int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16) % 10_000) / 10_000


@dataclass
class Decision:
    arrest: Arrest
    evidence: list[Evidence]
    p_submit: float


def decide(db, org: O.Org, svc: S.Service, npc_id: int, name: str, now: float, *, guards_present: int = 0,
           location: str | None = None, witnesses: list[int] | None = None, source: str = "sim") -> Decision | None:
    """The member says ``name`` is under arrest: the rules decide the answer. None when it means nothing (they are
    already held)."""
    cfg = config(org.key)
    if held(db, npc_id, now) is not None:
        return None
    ev = evidence(db, org, svc, npc_id, name, now)
    past = of_npc(db, npc_id)
    insisted = any(a.response == ARGUE and a.member_id == svc.member_id and now - a.gt <= cfg.insist_days for a in past)
    caught = any(a.response in (FLEE, FIGHT) and now - a.gt <= cfg.caught_days for a in past)
    strength = min(1.0, sum(e.strength for e in ev))
    attempt = len(past)
    if not ev and not insisted:
        response, p = ARGUE, 0.0
    else:
        if not ev:
            p = cfg.submit_wrongful
        else:
            p = (cfg.submit_base + cfg.submit_per_evidence * strength + cfg.submit_guards * min(guards_present, 2) / 2
                 + cfg.submit_per_rank * org.rank_index(svc.rank_key) + (cfg.submit_caught if caught else 0.0)
                 - (cfg.tough_penalty if _tough(db, npc_id, name, cfg) else 0.0))
        p = max(0.05, min(0.97, p))
        roll = _roll(org.key, npc_id, svc.service_id, attempt, round(now, 3))
        if roll < p:
            response = SUBMIT
        else:
            share = cfg.fight_share_tough if _tough(db, npc_id, name, cfg) else cfg.fight_share
            response = FIGHT if _roll("fight", npc_id, attempt, round(now, 3)) < share else FLEE
    charge = max(ev, key=lambda e: e.strength).charge if ev else None
    incident = next((e.incident for e in ev if e.incident is not None), None)
    days = cfg.sentences.get(charge, cfg.default_days) if charge else cfg.default_days
    payload = {"org": org.key, "service_id": svc.service_id, "npc": npc_id, "name": name, "charge": charge,
               "evidence": [e.why for e in ev], "strength": round(strength, 3), "wrongful": not ev,
               "response": response, "incident": incident, "days": days, "p_submit": round(p, 3),
               "caught": caught}
    event_id = S.event(db, "arrest", [svc.member_id, npc_id], payload, now, location, witnesses, source)
    return Decision(get(db, event_id), ev, p)


# ---- the game's jail records ----

def record_jail(db, arrest_id: int, via: str, gt: float, location: str | None = None) -> int | None:
    """The game locked the prisoner up (``jailed``) or let them out (``released``). Once each."""
    a = get(db, arrest_id)
    if a is None or via not in (JAILED, RELEASED):
        return None
    if via == JAILED:
        if jailed(db, a) is not None or released(db, a) is not None:
            return None
        return S.event(db, JAILED, [a.member_id, a.npc_id], {"org": a.org_key, "arrest": a.event_id,
                                                              "until": gt + a.days}, gt, location, [])
    if released(db, a) is not None:
        return None
    j = jailed(db, a)
    why = "wrongful" if a.wrongful and booked(db, a) is not None else "served" if j is not None else "escaped"
    return S.event(db, RELEASED, [a.npc_id], {"org": a.org_key, "arrest": a.event_id, "why": why}, gt, location, [])


def slipped(db, now: float) -> list[int]:
    """Prisoners never brought in within ``escort_days`` slip away (once each)."""
    out = []
    for row in db.execute("SELECT * FROM v_visible_event WHERE type = 'guard.arrest' AND"
                          " json_extract(payload_json, '$.response') = 'submit' AND game_time >= ?", (now - 30,)).fetchall():
        a = _row(row)
        if status(db, a, now) == "escorting" and now - a.gt > config(a.org_key).escort_days:
            S.event(db, RELEASED, [a.npc_id], {"org": a.org_key, "arrest": a.event_id, "why": "escaped"},
                    a.gt + config(a.org_key).escort_days, None, [])
            out.append(a.event_id)
    return out


# ---- booking with the superior ----

@dataclass
class Booking:
    arrest: Arrest
    wrongful: bool
    merit: float


def book(db, org: O.Org, svc: S.Service, boss: int, now: float, location: str | None = None,
         witnesses: list[int] | None = None) -> list[Booking]:
    """Book every prisoner the member brought in and has not booked yet. A wrongful arrest is a reprimand and the
    prisoner is let out (the caller tells the game)."""
    from livingworld.guard import duty as DU, incident as INC
    cfg = config(org.key)
    out = []
    for a in by_member(db, svc.member_id):
        if a.org_key != org.key or a.response != SUBMIT or jailed(db, a) is None or booked(db, a) is not None:
            continue
        member = DU._name(db, svc.member_id)
        if a.wrongful:
            S.event(db, "booked", [svc.member_id, boss], {"org": org.key, "arrest": a.event_id, "wrongful": True},
                    now, location, witnesses)
            svc = DU.reprimand(db, svc, boss, f"arrested {a.name} with nothing against them", cfg.merit_wrongful, now,
                               witnesses=witnesses)
            S.event(db, RELEASED, [a.npc_id], {"org": org.key, "arrest": a.event_id, "why": "wrongful"}, now, location, [])
            DU._remember(db, a.npc_id, svc.member_id, f"{member} of {org.name} arrested me with no charge and had me "
                         f"locked up until their superior let me out.", -0.6, 0.6, None, location, now)
            DU._remember(db, boss, svc.member_id, f"{member} locked up {a.name} with nothing against them; I let "
                         f"{a.name} out.", -0.35, 0.5, None, location, now)
            out.append(Booking(a, True, cfg.merit_wrongful))
            continue
        merit = cfg.merit_booked + (cfg.merit_suspect if a.incident is not None else 0.0)
        S.event(db, "booked", [svc.member_id, boss], {"org": org.key, "arrest": a.event_id, "wrongful": False},
                now, location, witnesses)
        svc = S.change(db, svc, f"brought in {a.name} for {a.charge}", now, d_merit=merit)
        inc = INC.get(db, a.incident) if a.incident is not None else None
        S.event(db, "arrested", [svc.member_id, boss], {"org": org.key, "incident": a.incident, "culprit": a.name,
                                                         "place": inc.place if inc else (f"in {a.location}" if a.location else ""),
                                                         "title": f"arrest {a.name} for {a.charge}", "arrest": a.event_id},
                now, location, witnesses)
        DU._remember(db, a.npc_id, svc.member_id, f"{member} of {org.name} arrested me for {a.charge} and locked me "
                     f"in the cells.", -0.5, 0.6, None, location, now)
        out.append(Booking(a, False, merit))
    return out


def prisoners(db, org: O.Org, now: float) -> list[tuple[Arrest, float]]:
    """Who sits in the hold's cells now, with when they get out."""
    out = []
    for row in db.execute("SELECT * FROM v_visible_event WHERE type = 'guard.arrest' AND"
                          " json_extract(payload_json, '$.org') = ? AND game_time >= ?", (org.key, now - 60)).fetchall():
        a = _row(row)
        j = jailed(db, a)
        if j is not None and status(db, a, now) == "jailed":
            out.append((a, float(j["until"])))
    return out
