"""How other Living World systems read profiles, so they agree with them instead of keeping their own numbers.

Romance (``social/romance.py``) keeps drive, romantic, shy, flirtatious, monogamy, intimacy_boundary and
attracted_to per person. For a woman with an active profile those come from the profile:

    drive = libido / 100          romantic  = romantic_inclination / 100   shy = 1 - sexual_confidence / 100
    flirtatious = initiation / 100   monogamy = exclusivity / 100
    intimacy_boundary = intimacy_boundary   attracted_to = attracted_to_sex

Profiles are generated from the romance seed with the inverse mapping, so seeded people keep exactly the numbers
they had. Consent (``social/consent.py``) reads these romance traits and nothing else from here: libido feeds only
the wish term, and never outweighs a hard limit; a boundary set in the profile is the boundary consent applies.
"""
from __future__ import annotations

import logging
import os

from livingworld.personality.config import Settings

logger = logging.getLogger(__name__)

ENV = "MANTELLA_LW_PERSONALITY"
_settings_source = None     # a callable returning Settings, set by the running service


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def use_settings(source) -> None:
    """Point the bridge at the running settings (``SettingsFile.get``), or None to switch it off."""
    global _settings_source
    _settings_source = source


def current_settings() -> Settings | None:
    if _settings_source is None or not enabled():
        return None
    return _settings_source()


def romance_traits(db, npc_id: int, traits: dict, now: float | None = None) -> dict:
    """``traits`` with the profile's values laid over them (unchanged when she has no active profile)."""
    settings = current_settings()
    if settings is None or not settings.enabled:
        return traits
    try:
        from livingworld.personality.profile import resolve
        if now is None:
            now = db.execute("SELECT COALESCE(MAX(game_time), 0) FROM world_event").fetchone()[0]
        p = resolve(db, npc_id, settings, now)
        if not p.active:
            return traits
        out = dict(traits)
        v = p.values
        if "libido" in v:
            out["drive"] = v["libido"] / 100
        if "romantic_inclination" in v:
            out["romantic"] = v["romantic_inclination"] / 100
        if "sexual_confidence" in v:
            out["shy"] = round(1 - v["sexual_confidence"] / 100, 4)
        if "initiation" in v:
            out["flirtatious"] = v["initiation"] / 100
        if "exclusivity" in v:
            out["monogamy"] = v["exclusivity"] / 100
        if "intimacy_boundary" in v:
            out["intimacy_boundary"] = v["intimacy_boundary"]
        if v.get("attracted_to_sex"):
            out["attracted_to"] = list(v["attracted_to_sex"])
        return out
    except Exception:
        logger.exception("Living World personality: could not read a profile for romance; using romance's own traits")
        return traits
