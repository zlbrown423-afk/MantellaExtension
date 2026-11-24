"""The game's territory records.

    v=1|key=812345-9|type=territory|via=occupied|gt=12.5|site=halted_stream_camp|owner=thalmor|prev=unoccupied
      |strength=5|contested=0|since=12.5|w=<witness>

``via`` for a site: occupied, reinforced, cleared, withdrawn, established, contested, abandoned, sighted.
``cooldown`` (cleared): no reclamation before that game time. ``since``: when the current owner took the site.
``via`` for a soldier: assign, recall, dead, replace, transfer (``to`` = the new site), plus ``npc=<actor>``,
``faction``, ``rank``, ``squad``, ``duty``, ``equip`` (items joined by ``;``) and ``hp`` (0..1).
Witnesses (``w=``) are the people who were near and could see; the game never lists a child.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.perception.records import BadRecord, GameActor, Witness, _actor, _witness
from livingworld.territory import FACTIONS, OWNERS
from livingworld.territory.sites import by_key

SITE_VIAS = ("occupied", "reinforced", "cleared", "withdrawn", "established", "contested", "abandoned", "sighted")
GARRISON_VIAS = ("assign", "recall", "dead", "replace", "transfer")


@dataclass
class TerritoryRecord:
    key: str
    via: str
    game_time: float
    site: str
    owner: str | None = None
    prev: str | None = None
    strength: int = 0
    contested: bool = False
    since: float | None = None
    cooldown: float | None = None
    npc: GameActor | None = None
    faction: str | None = None
    rank: str = "soldier"
    squad: str | None = None
    duty: str | None = None
    equip: list[str] = field(default_factory=list)
    hp: float | None = None
    to: str | None = None
    witnesses: list[Witness] = field(default_factory=list)
    raw: str = ""

    @property
    def is_garrison(self) -> bool:
        return self.via in GARRISON_VIAS


def is_territory_record(raw: str) -> bool:
    low = (raw or "").lower()
    return "|type=territory|" in low or low.endswith("|type=territory")


def parse(text: str) -> TerritoryRecord:
    fields: dict[str, str] = {}
    witnesses: list[Witness] = []
    for part in text.split("|"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip().lower()
        if name == "w":
            witnesses.append(_witness(value))
        else:
            fields[name] = value
    if fields.get("v") != "1":
        raise BadRecord(f"unknown record version {fields.get('v')!r}")
    for need in ("key", "via", "gt", "site"):
        if not fields.get(need):
            raise BadRecord(f"territory record has no {need}")
    via = fields["via"].strip().lower()
    if via not in SITE_VIAS + GARRISON_VIAS:
        raise BadRecord(f"unknown territory via {via!r}")
    site = fields["site"].strip().lower()
    if by_key(site) is None:
        raise BadRecord(f"unknown territory site {site!r}")
    rec = TerritoryRecord(key=fields["key"], via=via, game_time=float(fields["gt"]), site=site, witnesses=witnesses,
                          raw=text)
    for name in ("owner", "prev"):
        value = (fields.get(name) or "").strip().lower() or None
        if value is not None and value not in OWNERS:
            raise BadRecord(f"unknown owner {value!r}")
        setattr(rec, name, value)
    rec.strength = max(0, int(float(fields.get("strength") or 0)))
    rec.contested = fields.get("contested") == "1"
    rec.since = float(fields["since"]) if fields.get("since") else None
    rec.cooldown = float(fields["cooldown"]) if fields.get("cooldown") else None
    if via in SITE_VIAS and via != "sighted" and rec.owner is None:
        raise BadRecord(f"a {via} record needs owner")
    if via == "cleared" and rec.prev in (None, "unoccupied"):
        raise BadRecord("a cleared record needs prev (who was driven out)")
    if rec.is_garrison:
        if not fields.get("npc"):
            raise BadRecord(f"a {via} record needs npc")
        rec.npc = _actor(fields["npc"])
        faction = (fields.get("faction") or "").strip().lower()
        if via in ("assign", "transfer", "replace") and faction not in FACTIONS:
            raise BadRecord(f"a {via} record needs faction thalmor or wardens")
        rec.faction = faction or None
        rec.rank = (fields.get("rank") or "soldier").strip().lower()
        rec.squad = (fields.get("squad") or "").strip() or None
        rec.duty = (fields.get("duty") or "").strip() or None
        rec.equip = [e.strip() for e in (fields.get("equip") or "").split(";") if e.strip()]
        rec.hp = max(0.0, min(1.0, float(fields["hp"]))) if fields.get("hp") else None
        to = (fields.get("to") or "").strip().lower() or None
        if via == "transfer" and (to is None or by_key(to) is None):
            raise BadRecord("a transfer record needs a known to= site")
        rec.to = to
    return rec
