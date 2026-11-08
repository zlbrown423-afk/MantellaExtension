"""Who belongs to which hold's guard, and the personal names of generic guards.

The game reports each conversation participant's ``IsGuard()`` and crime faction. A guard whose crime
faction is a configured hold's joins that guard's roster here (``guard_profile``). Generic guards all
share one name in game ("Whiterun Guard"), so each one the player meets gets a stable personal name,
picked from the lists below by its reference ID: the same guard always has the same name, and two
guards of one hold never share a name. Seeded leaders (Commander Caius) keep their own names.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from livingworld.guard import orgs as O
from livingworld.store.minors import is_child

MALE = ("Hrolf", "Agnar", "Bjorn", "Eirik", "Geirmund", "Hakon", "Ingvar", "Jorund", "Ketil", "Leif",
        "Njal", "Orm", "Ragnvald", "Sigurd", "Torsten", "Ulfgar", "Arnbjorn", "Asgeir", "Brand", "Dagur",
        "Egil", "Finnr", "Gunnar", "Halvar", "Ivar", "Kolbeinn", "Ljot", "Rorik", "Skuli", "Thrain",
        "Varin", "Yngvar", "Marcus Tullo", "Quintus Avel", "Lucius Varro", "Atar", "Ravam", "Drogan", "Orvald", "Steinar")
FEMALE = ("Astrid", "Brynja", "Dagny", "Eydis", "Freydis", "Gudrun", "Hilde", "Ingrid", "Jorunn", "Kari",
          "Liv", "Ragna", "Sigrun", "Thora", "Ulfhild", "Valdis", "Asa", "Bera", "Edda", "Frida",
          "Gyda", "Halla", "Idunn", "Rannveig", "Signy", "Tove", "Una", "Yrsa", "Livia Sabine", "Aurelia Crassa")


@dataclass(frozen=True)
class GuardFacts:
    """What the game told us about one participant (Mantella-Spell LIVINGWORLD PATCH: guard fields)."""
    is_guard: bool = False
    crime_plugin: str | None = None
    crime_faction: str | None = None
    bounty: int | None = None          # the player's bounty in that crime faction, when the game sent it


@dataclass(frozen=True)
class Profile:
    npc_id: int
    org_key: str
    given_name: str | None
    is_leader: bool
    leader_rank: str | None
    recruits: bool
    role: str


def _pick(seed: str, names: tuple[str, ...], taken: set[str]) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    for i in range(len(names)):
        name = names[(h + i) % len(names)]
        if name not in taken:
            return name
    n = 2
    while True:   # every name in use: add a byname
        name = f"{names[h % len(names)]} the {('Younger', 'Elder', 'Tall', 'Quiet', 'Red')[n % 5]}"
        if name not in taken:
            return name
        n += 1


def _npc(db, npc_id: int):
    return db.execute("SELECT npc_id, plugin, base_form_id, ref_form_id, display_name, sex, is_generic, is_player"
                      " FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()


def note(db, npc_id: int, facts: GuardFacts | None, gt: float) -> Profile | None:
    """Record what we learned about this person. Returns their guard profile, if they have one."""
    row = _npc(db, npc_id)
    if row is None or row["is_player"] or is_child(db, npc_id):
        return None
    led = O.leader_of(row["plugin"], row["base_form_id"])
    if led is not None:
        org, person = led
        db.execute("INSERT OR IGNORE INTO guard_profile (npc_id, org_key, given_name, first_seen_gt) VALUES (?,?,NULL,?)",
                   (npc_id, org.key, gt))
        return get(db, npc_id)
    if facts is None or not facts.is_guard:
        return get(db, npc_id)
    org = O.by_crime_faction(facts.crime_plugin, facts.crime_faction)
    if org is None:
        return get(db, npc_id)
    existing = get(db, npc_id)
    if existing is not None:
        return existing
    given = None
    if row["is_generic"] or row["display_name"].lower() == org.guard_label.lower() or "guard" in row["display_name"].lower():
        taken = {r[0] for r in db.execute("SELECT given_name FROM guard_profile WHERE org_key = ? AND given_name IS NOT NULL",
                                          (org.key,))}
        names = FEMALE if row["sex"] == "female" else MALE
        given = _pick(f"{org.key}|{row['plugin']}|{row['ref_form_id'] or row['base_form_id']}", names, taken)
    db.execute("INSERT INTO guard_profile (npc_id, org_key, given_name, first_seen_gt) VALUES (?,?,?,?)",
               (npc_id, org.key, given, gt))
    return get(db, npc_id)


def get(db, npc_id: int) -> Profile | None:
    r = db.execute("SELECT p.npc_id, p.org_key, p.given_name, n.plugin, n.base_form_id FROM guard_profile p"
                   " JOIN npc n ON n.npc_id = p.npc_id WHERE p.npc_id = ? AND n.is_child = 0", (npc_id,)).fetchone()
    if r is None:
        return None
    led = O.leader_of(r["plugin"], r["base_form_id"])
    person = led[1] if led and led[0].key == r["org_key"] else None
    return Profile(r["npc_id"], r["org_key"], r["given_name"], person is not None,
                   person.rank if person else None, bool(person and person.recruits), person.role if person else "")


def roster(db, org_key: str) -> list[Profile]:
    return [p for p in (get(db, r[0]) for r in db.execute(
        "SELECT npc_id FROM guard_profile WHERE org_key = ? ORDER BY npc_id", (org_key,))) if p is not None]


def leader_id(db, org: O.Org, key: str) -> int | None:
    person = org.leader(key) or (org.jarl if org.jarl and org.jarl.key == key else None)
    if person is None:
        return None
    row = db.execute("SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? AND is_child = 0 ORDER BY npc_id"
                     " LIMIT 1", (person.plugin, person.base_id)).fetchone()
    return row[0] if row else None
