"""Hold guard organisations, read from ``data/orgs.json`` (see the file's ``_about`` notes)."""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

ORG_FILES = ("orgs.json",)


def local_id(raw: str | int | None) -> str | None:
    """'0x00038257' from '38257', '0x38257', 0x38257; None stays None."""
    if raw is None or raw == "":
        return None
    value = int(raw, 16) if isinstance(raw, str) else int(raw)
    return f"0x{value & 0x00FFFFFF:08X}"


@dataclass(frozen=True)
class Rank:
    key: str
    title: str
    seats: int | None = None
    min_days: float = 0.0


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    plugin: str
    base_id: str
    rank: str | None = None
    recruits: bool = False
    role: str = ""


@dataclass(frozen=True)
class Item:
    name: str
    plugin: str
    form_id: str | None


@dataclass(frozen=True)
class Org:
    key: str
    name: str
    hold: str
    guard_label: str
    crime_plugin: str
    crime_faction: str | None
    culture: str
    jarl: Person | None
    leaders: tuple[Person, ...]
    ranks: tuple[Rank, ...]
    probation_days: float
    recruit: dict[str, float]
    uniform: dict[str, tuple[Item, ...]] = field(default_factory=dict, compare=False, hash=False)
    generic_rank_key: str | None = None   # the rank an ordinary guard of this hold holds (default: the second rank)
    rank_note: str = ""                   # one sentence for prompts about what the rank titles mean
    duty_file: str | None = None          # guard/data/<file> with this guard's duties
    pay: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    quarters: str = ""                    # where a member sleeps and keeps their things

    @property
    def generic_rank(self) -> str:
        if self.generic_rank_key and self.rank(self.generic_rank_key):
            return self.generic_rank_key
        return self.ranks[min(1, len(self.ranks) - 1)].key

    def rank(self, key: str | None) -> Rank | None:
        return next((r for r in self.ranks if r.key == key), None)

    def title(self, key: str | None) -> str:
        r = self.rank(key)
        return r.title if r else (key or "member")

    def a_title(self, key: str | None) -> str:
        """The title with its article: "a Recruit", "an Optio"."""
        t = self.title(key)
        return ("an " if t[:1].lower() in "aeiou" else "a ") + t

    def rank_index(self, key: str | None) -> int:
        return next((i for i, r in enumerate(self.ranks) if r.key == key), -1)

    def next_rank(self, key: str | None) -> Rank | None:
        i = self.rank_index(key)
        return self.ranks[i + 1] if 0 <= i < len(self.ranks) - 1 else None

    def leader(self, key: str) -> Person | None:
        return next((p for p in self.leaders if p.key == key), None)

    def wage(self, rank_key: str | None) -> int:
        return int((self.pay.get("per_week") or {}).get(rank_key or "", 0))

    @property
    def dock(self) -> int:
        return int(self.pay.get("dock_per_reprimand", 0))

    @property
    def gold(self) -> Item | None:
        g = self.pay.get("gold")
        return Item(g.get("name", "Gold"), g.get("plugin", "Skyrim.esm"), local_id(g.get("id"))) if g else None

    def superior(self, rank_key: str | None) -> Person | None:
        """The leader who gives a member of this rank their orders: the lowest-ranked leader above them."""
        mine = self.rank_index(rank_key)
        above = sorted((p for p in self.leaders if self.rank_index(p.rank) > mine), key=lambda p: self.rank_index(p.rank))
        return above[0] if above else None

    def setting(self, name: str, default: float) -> float:
        return float(self.recruit.get(name, default))


def _person(raw: dict[str, Any] | None) -> Person | None:
    if not raw:
        return None
    return Person(key=raw["key"], name=raw.get("name", raw["key"]), plugin=raw.get("plugin", "Skyrim.esm"),
                  base_id=local_id(raw["base_id"]), rank=raw.get("rank"), recruits=bool(raw.get("recruits")),
                  role=raw.get("role", ""))


def _org(raw: dict[str, Any]) -> Org:
    crime = raw.get("crime_faction") or {}
    uniform = {rank: tuple(Item(i["name"], i.get("plugin", "Skyrim.esm"), local_id(i.get("id"))) for i in items)
               for rank, items in (raw.get("uniform") or {}).items()}
    return Org(key=raw["key"], name=raw["name"], hold=raw["hold"], guard_label=raw.get("guard_label", raw["hold"] + " Guard"),
               crime_plugin=crime.get("plugin", "Skyrim.esm"), crime_faction=local_id(crime.get("id")),
               culture=raw.get("culture", ""), jarl=_person(raw.get("jarl")),
               leaders=tuple(_person(p) for p in raw.get("leaders") or ()),
               ranks=tuple(Rank(r["key"], r["title"], r.get("seats"), float(r.get("min_days", 0)))
                           for r in raw["ranks"]),
               probation_days=float(raw.get("probation_days", 7)), recruit=dict(raw.get("recruit") or {}),
               uniform=uniform, generic_rank_key=raw.get("generic_rank"), rank_note=raw.get("rank_note", ""),
               duty_file=raw.get("duties"), pay=dict(raw.get("pay") or {}), quarters=raw.get("quarters", ""))


@functools.lru_cache(maxsize=1)
def orgs() -> tuple[Org, ...]:
    data = resources.files("livingworld.guard").joinpath("data")
    out = []
    for name in ORG_FILES:
        for raw in json.loads(data.joinpath(name).read_text(encoding="utf-8"))["orgs"]:
            out.append(_org(raw))
    return tuple(out)


def by_key(key: str | None) -> Org | None:
    if not key:
        return None
    k = key.lower().strip()
    return next((o for o in orgs() if o.key == k or o.hold.lower() == k or o.key.split("_")[0] == k), None)


def by_crime_faction(plugin: str | None, form_id: str | int | None) -> Org | None:
    fid = local_id(form_id) if form_id not in (None, "", 0) else None
    if fid is None:
        return None
    return next((o for o in orgs() if o.crime_faction == fid and (plugin is None or o.crime_plugin == plugin)), None)


def leader_of(plugin: str, base_id: str) -> tuple[Org, Person] | None:
    """The org and leader entry for a seeded leader (or the Jarl, with rank None), matched by base form ID."""
    for o in orgs():
        for p in o.leaders + ((o.jarl,) if o.jarl else ()):
            if p.plugin == plugin and p.base_id == base_id:
                return o, p
    return None


def uniform_for(org: Org, rank_key: str | None) -> tuple[Item, ...]:
    """Items issued on reaching ``rank_key``."""
    return org.uniform.get(rank_key or "", ())
