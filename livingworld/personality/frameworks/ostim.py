"""OStim Standalone adapter (checked against 7.5.1b, the version installed: 86 actions, 280 scenes).

Reads the framework's own data files, never a hard-coded scene list:
* ``<Data>/SKSE/Plugins/OStim/actions/<id>.json`` - the action ids (file name = id, as OStim itself loads them);
* ``<Data>/SKSE/Plugins/OStim/scenes/**/<id>.json`` - scenes: ``actors[].intendedSex``,
  ``actions[] {type, actor, target}``, ``tags``, ``furniture`` (file name = scene id).
The folder comes from ``MANTELLA_LW_OSTIM_FOLDER`` or the game's Data folder from Mantella's config.

``ACTION_GROUPS`` maps every action id OStim 7.5.1b ships to an activity group. An id it does not know (an
animation pack's own action) is ``unknown`` and judged not compatible until mapped by hand in the settings file
(``"intimacy": {"framework_map": {"someaction": "kissing"}}`` at the top level).
The live game already sends the action ids of a running scene (``mantella_lw_scene`` ``acts=``), which is what
the in-scene lines use; this adapter is what lets them be read as preferences.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

from livingworld.personality.frameworks.base import NONE, UNKNOWN, SceneAction, SceneInfo

logger = logging.getLogger(__name__)

FOLDER_ENV = "MANTELLA_LW_OSTIM_FOLDER"

ACTION_GROUPS: dict[str, str] = {
    # affection
    "hugging": "affection", "cuddling": "affection", "spooning": "affection", "holdingbody": "affection",
    "leglocking": "affection",
    "holdinghand": "hand_holding",
    "kissing": "kissing", "frenchkissing": "kissing", "kissingcheek": "kissing", "kissinghand": "kissing",
    "kissingneck": "kissing", "kissingfoot": "kissing",
    "strokinghead": "caressing", "pattinghead": "caressing", "holdingchin": "caressing", "holdinghead": "caressing",
    "holdinghip": "caressing", "holdingthigh": "caressing", "holdingleg": "caressing", "holdingfoot": "caressing",
    "holdingneck": "caressing", "ticklingfoot": "caressing", "breastsliding": "caressing",
    "massaging": "massage", "washing": "massage", "bathing": "massage",
    "gropingbreast": "groping", "gropingbutt": "groping", "gropingtesticles": "groping",
    # sexual
    "handjob": "manual", "vaginalfingering": "manual", "oralfingering": "manual", "footjob": "manual",
    "thighjob": "manual", "boobjob": "manual", "buttjob": "manual", "grindingpenis": "manual",
    "grindingthigh": "manual", "grindingfoot": "manual", "grindingobject": "manual", "vulvalrubbing": "manual",
    "breastsmothering": "manual", "buttsmothering": "manual", "rubbingpenisagainstface": "manual",
    "blowjob": "oral", "deepthroating": "oral", "penilelicking": "oral", "testicularlicking": "oral",
    "vulvaleating": "oral", "vulvallicking": "oral", "lickingnipple": "oral", "suckingnipple": "oral",
    "vaginalsex": "intercourse", "tribbing": "intercourse", "mounting": "intercourse",
    "analsex": "anal", "analfingering": "anal", "anallicking": "anal", "rimjob": "anal", "analfisting": "anal",
    "vaginaltoying": "toys", "oraltoying": "toys", "analtoying": "toys", "vaginalfisting": "toys",
    "spanking": "rough", "breastslapping": "rough", "choking": "rough", "pullinghair": "rough",
    "cumonbutt": "climax_on_body", "cumonchest": "climax_on_body", "cumonvulva": "climax_on_body",
    "facial": "climax_on_body",
    "femalemasturbation": "solo", "malemasturbation": "solo",
    "tailjob": "tail", "analtailsex": "tail", "vaginaltailsex": "tail", "massagingtail": "tail",
    "strokingtail": "tail", "pullingtail": "tail",
    "vampirebiting": "vampire",
    # not intimate
    "holdingarm": NONE, "dancing": NONE, "reading": NONE, "spectating": NONE, "default": NONE,
}


@lru_cache(maxsize=1)
def known_aliases() -> dict[str, str]:
    """OStim 7.5.1b's own alias table (alias -> action id), for when the installed folder cannot be read."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ostim_aliases.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {str(k).lower(): str(v).lower() for k, v in json.load(fh)["aliases"].items()}
    except (OSError, ValueError, KeyError):
        return {}


def data_folder() -> str | None:
    folder = os.environ.get(FOLDER_ENV)
    if folder:
        return folder
    try:
        from src import utils
        mod = getattr(utils, "configured_mod_folder", None)
    except Exception:
        mod = None
    return os.path.join(mod, "SKSE", "Plugins", "OStim") if mod else None


class OStim:
    name = "OStim Standalone"

    def __init__(self, folder: str | None = None, extra_map: dict[str, str] | None = None) -> None:
        self.folder = folder or data_folder()
        self.extra_map = {k.lower(): v for k, v in (extra_map or {}).items()}
        self._actions: set[str] | None = None
        self._aliases: dict[str, str] = {}
        self._scenes: dict[str, SceneInfo] | None = None

    def available(self) -> bool:
        return bool(self.folder) and os.path.isdir(os.path.join(self.folder, "actions"))

    def actions(self) -> set[str]:
        if self._actions is None:
            self._actions, self._aliases = set(), {}
            if self.available():
                folder = os.path.join(self.folder, "actions")
                for f in os.listdir(folder):
                    if not f.lower().endswith(".json"):
                        continue
                    action = f[:-5].lower()
                    self._actions.add(action)
                    try:   # scenes may name an action by one of its aliases ("kiss" for kissing), as OStim allows
                        with open(os.path.join(folder, f), encoding="utf-8-sig") as fh:
                            data = json.load(fh)
                        for alias in ((data.get("aliases") or []) if isinstance(data, dict) else []):
                            self._aliases[str(alias).lower()] = action
                    except (OSError, ValueError):
                        logger.warning(f"OStim adapter: could not read action {f}; its aliases are unknown")
        return self._actions

    def canonical(self, action: str) -> str:
        self.actions()
        a = (action or "").lower()
        if a in self._aliases:
            return self._aliases[a]
        if a in ACTION_GROUPS or a in self.extra_map or (self._actions and a in self._actions):
            return a
        return known_aliases().get(a, a)

    def group_of(self, action: str) -> str:
        a = self.canonical(action)
        if a in self.extra_map:
            return self.extra_map[a]
        if a in ACTION_GROUPS:
            return ACTION_GROUPS[a]
        return UNKNOWN

    def scenes(self) -> dict[str, SceneInfo]:
        if self._scenes is None:
            self._scenes = _load_scenes(self.folder) if self.available() else {}
        return self._scenes

    def unmapped(self) -> list[str]:
        """Installed action ids this adapter cannot place (an animation pack's own); shown by the admin tool."""
        return sorted(a for a in self.actions() if self.group_of(a) == UNKNOWN)


def _load_scenes(folder: str) -> dict[str, SceneInfo]:
    out: dict[str, SceneInfo] = {}
    root = os.path.join(folder, "scenes")
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                logger.warning(f"OStim adapter: could not read scene {f}; skipped")
                continue
            actors = tuple(str(a.get("intendedSex") or "") for a in data.get("actors") or [] if isinstance(a, dict))
            actions = tuple(SceneAction(str(a.get("type") or "").lower(), a.get("actor"), a.get("target"))
                            for a in data.get("actions") or [] if isinstance(a, dict))
            out[f[:-5]] = SceneInfo(f[:-5], actors, actions, tuple(data.get("tags") or ()), str(data.get("furniture") or ""))
    return out


@lru_cache(maxsize=4)
def installed(folder: str | None = None) -> OStim:
    return OStim(folder)
