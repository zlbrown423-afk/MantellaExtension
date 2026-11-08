"""Starting communities, memberships, personalities and relationships from ``data/*.json``.

People are matched by plugin and base form ID, so a seed entry takes effect once that NPC has been
seen in play (has an ``npc`` row). ``apply`` is cheap and idempotent; it runs at each refresh and only
writes when someone new has turned up. Seeded values are the base of each relationship row: what
happens in play is stored as ``relationship_change`` rows on top, so a seed never erases history.
Children are never seeded (see ``store/minors.py``).
"""
from __future__ import annotations

import functools
import json
from importlib import resources
from typing import Any

from livingworld.store.minors import looks_like_child

SEED_FILES = ("riverwood.json",)


@functools.lru_cache(maxsize=1)
def seeds() -> tuple[dict[str, Any], ...]:
    data = resources.files("livingworld.social").joinpath("data")
    return tuple(json.loads(data.joinpath(name).read_text(encoding="utf-8")) for name in SEED_FILES)


def _local_id(base_id: str) -> str:
    return "0x" + base_id.upper().rjust(8, "0")


def gossipiness() -> dict[tuple[str, str], float]:
    """(plugin, base_form_id) -> gossipy 0..1 for seeded people."""
    out = {}
    for seed in seeds():
        for p in seed["people"]:
            out[(p["plugin"], _local_id(p["base_id"]))] = float(p.get("gossipy", 0.5))
    return out


def romance_traits() -> dict[tuple[str, str], dict[str, Any]]:
    """(plugin, base_form_id) -> the seed's romance traits for that person (see "_romance_about")."""
    out = {}
    for seed in seeds():
        romance = seed.get("romance") or {}
        for p in seed["people"]:
            if p["key"] in romance:
                out[(p["plugin"], _local_id(p["base_id"]))] = dict(romance[p["key"]])
    return out


def _person_ids(db, seed: dict[str, Any]) -> dict[str, int]:
    found = {}
    for p in seed["people"]:
        local = _local_id(p["base_id"])
        if looks_like_child(local):
            continue
        row = db.execute("SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? AND is_child = 0"
                         " ORDER BY npc_id LIMIT 1", (p["plugin"], local)).fetchone()
        if row:
            found[p["key"]] = row[0]
    return found


class Seeder:
    def __init__(self) -> None:
        self._applied: dict[str, frozenset[int]] = {}   # seed name -> npc ids it was last applied with

    def apply(self, db, game_time: float) -> int:
        """Apply every seed to the NPCs that exist now. Returns how many people were seeded."""
        total = 0
        for seed in seeds():
            ids = _person_ids(db, seed)
            signature = frozenset(ids.values())
            name = seed["communities"][0]["key"]
            if self._applied.get(name) == signature:
                continue
            self._apply(db, seed, ids, game_time)
            self._applied[name] = signature
            total += len(ids)
        return total

    @staticmethod
    def _apply(db, seed: dict[str, Any], ids: dict[str, int], gt: float) -> None:
        community_ids: dict[str, int] = {}
        for c in seed["communities"]:
            parent = community_ids.get(c.get("parent", ""))
            db.execute("INSERT INTO community (key, name, kind, location, parent_id) VALUES (?,?,?,?,?)"
                       " ON CONFLICT(key) DO UPDATE SET name = excluded.name, kind = excluded.kind,"
                       " location = excluded.location, parent_id = excluded.parent_id",
                       (c["key"], c["name"], c["kind"], c.get("location"), parent))
            community_ids[c["key"]] = db.execute("SELECT community_id FROM community WHERE key = ?",
                                                 (c["key"],)).fetchone()[0]
        for p in seed["people"]:
            npc_id = ids.get(p["key"])
            if npc_id is None:
                continue
            for key, m in (p.get("member_of") or {}).items():
                db.execute("INSERT INTO membership (npc_id, community_id, role, influence) VALUES (?,?,?,?)"
                           " ON CONFLICT(npc_id, community_id) DO UPDATE SET role = excluded.role,"
                           " influence = excluded.influence",
                           (npc_id, community_ids[key], m.get("role"), float(m.get("influence", 0.3))))
            seeded_traits = {"gossipy": float(p.get("gossipy", 0.5)), **(p.get("traits") or {})}
            if not db.execute("SELECT 1 FROM persona_version WHERE npc_id = ?", (npc_id,)).fetchone():
                db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                           " VALUES (?, 1, '', ?, 'author', ?)", (npc_id, json.dumps(seeded_traits), gt))
            else:
                # traits from the bio may have been written first (social/traits.py): the seed file wins
                for trait, value in seeded_traits.items():
                    db.execute("UPDATE persona_version SET traits_json = json_set(traits_json, ?, ?) WHERE npc_id = ?",
                               (f"$.{trait}", float(value), npc_id))
        for r in seed["relationships"]:
            a, b = ids.get(r["a"]), ids.get(r["b"])
            if a is None or b is None:
                continue
            back = r.get("back") or {}
            values = (float(r.get("trust", 0)), float(r.get("affinity", 0)), float(r.get("respect", 0)),
                      float(r.get("familiarity", 0)), float(r.get("attraction", 0)))
            _upsert(db, a, b, r.get("kinship"), r.get("status", "acquaintance"), values, gt)
            if not r.get("one_way"):
                _upsert(db, b, a, back.get("kinship", r.get("kinship")), back.get("status", r.get("status", "acquaintance")),
                        values, gt)
        for place in seed.get("places") or []:
            residents = [ids[k] for k in place.get("residents", []) if k in ids]
            staff = community_ids.get(place.get("staff", ""))
            db.execute("INSERT INTO place (place_key, name, location, kind, residents_json, staff_household_id, privacy,"
                       " child_in_home, bed_ref) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(place_key) DO UPDATE SET"
                       " name = excluded.name, location = excluded.location, kind = excluded.kind,"
                       " residents_json = excluded.residents_json, staff_household_id = excluded.staff_household_id,"
                       " privacy = excluded.privacy, child_in_home = excluded.child_in_home, bed_ref = excluded.bed_ref",
                       (place["key"], place["name"], place.get("location"), place["kind"], json.dumps(residents), staff,
                        float(place["privacy"]), int(bool(place.get("child_in_home"))), place.get("bed_ref")))


def _upsert(db, from_id: int, to_id: int, kinship: str | None, status: str, values: tuple, gt: float) -> None:
    trust, affinity, respect, familiarity, attraction = values
    db.execute("INSERT INTO relationship (from_id, to_id, kinship, status, trust, affinity, respect, familiarity,"
               " attraction, updated_gt) VALUES (?,?,?,?,?,?,?,?,?,?)"
               " ON CONFLICT(from_id, to_id) DO UPDATE SET kinship = excluded.kinship, status = excluded.status,"
               " trust = excluded.trust, affinity = excluded.affinity, respect = excluded.respect,"
               " familiarity = excluded.familiarity, attraction = excluded.attraction, updated_gt = excluded.updated_gt",
               (from_id, to_id, kinship, status, trust, affinity, respect, familiarity, attraction, gt))
