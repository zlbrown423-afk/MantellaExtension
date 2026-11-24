"""The registry of sites that can change hands (data/sites.json)."""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    location: str
    form_id: str
    plugin: str
    hold: str
    kind: str
    weight: float
    news_places: tuple[str, ...]


@functools.lru_cache(maxsize=1)
def all_sites() -> tuple[Site, ...]:
    raw = json.loads(resources.files("livingworld.territory").joinpath("data", "sites.json").read_text(encoding="utf-8"))
    return tuple(Site(s["key"], s["name"], s.get("location", s["name"]), s["form_id"], s.get("plugin", "Skyrim.esm"),
                      s.get("hold", ""), s.get("kind", ""), float(s.get("weight", 0.5)),
                      tuple(s.get("news_places") or ())) for s in raw["sites"])


def by_key(key: str | None) -> Site | None:
    k = (key or "").strip().lower()
    return next((s for s in all_sites() if s.key == k), None)


def by_form_id(form_id: str | None, plugin: str = "Skyrim.esm") -> Site | None:
    want = (form_id or "").strip().lower().replace("0x", "").lstrip("0")
    return next((s for s in all_sites() if s.plugin.lower() == plugin.lower()
                 and s.form_id.lower().replace("0x", "").lstrip("0") == want), None)
