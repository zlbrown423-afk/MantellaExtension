"""Administration: inspect, history, export / import, reset, regenerate, enable / disable.

Used by the command line tool (``python -m livingworld.personality``) and by "debug profile" in a conversation.
Hand-set values are written to the settings file only (``config.py``); the world database keeps the generated
starting point, the change history and an audit row for every admin action (``personality_admin``).
"""
from __future__ import annotations

import json
from typing import Any

from livingworld.personality import discovery, generate, schema
from livingworld.personality import profile as P
from livingworld.personality.config import NpcSettings, Settings, SettingsFile, normal_id


def audit(db, npc_id: int | None, action: str, detail: dict | None = None) -> None:
    db.execute("INSERT INTO personality_admin (npc_id, action, detail_json) VALUES (?,?,?)",
               (npc_id, action, json.dumps(detail or {})))


def find(db, who: str) -> list[int]:
    """npc ids for a name ('Gerdur', case ignored) or 'Plugin.esm|0x0001347C'."""
    who = (who or "").strip()
    if "|" in who:
        plugin, _, fid = who.partition("|")
        return [r[0] for r in db.execute("SELECT npc_id FROM npc WHERE lower(plugin) = lower(?) AND base_form_id = ?",
                                         (plugin.strip(), normal_id(fid)))]
    return [r[0] for r in db.execute("SELECT npc_id FROM npc WHERE lower(display_name) = lower(?) AND is_player = 0",
                                     (who,))]


def key_for(db, npc_id: int) -> str:
    row = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return f"{row[0]}|{row[1]}"


def history(db, npc_id: int) -> list[dict[str, Any]]:
    out = []
    for r in db.execute("SELECT attribute, kind, delta, value_json, reason, cause, game_time, real_time"
                        " FROM v_personality_change WHERE npc_id = ? AND kind <> 'checkpoint'"
                        " ORDER BY game_time, change_id", (npc_id,)):
        out.append({"attribute": r[0], "kind": r[1], "delta": r[2],
                    "item": json.loads(r[3]) if r[3] else None, "reason": r[4], "cause": r[5],
                    "game_day": round(r[6], 3), "when": r[7]})
    for r in db.execute("SELECT action, detail_json, real_time FROM personality_admin WHERE npc_id = ?"
                        " ORDER BY admin_id", (npc_id,)):
        out.append({"admin": r[0], "detail": json.loads(r[1]), "when": r[2]})
    return out


def inspect(db, settings: Settings, npc_id: int, now: float, player_id: int | None = None) -> dict[str, Any]:
    """Everything about her profile: effective values, where each came from, change so far, what the player knows.
    Read-only, except that a first look generates and stores her starting point."""
    p = P.resolve(db, npc_id, settings, now)
    out: dict[str, Any] = {"name": p.name, "key": key_for(db, npc_id), "eligible": p.eligible, "why": p.why,
                           "enabled": p.enabled, "mode": p.mode}
    if not p.active:
        return out
    out.update({"values": p.values, "sources": p.sources, "manual": p.manual, "generated": p.generated,
                "drift": p.drift, "added_over_time": p.added, "private": sorted(p.private),
                "history": history(db, npc_id)})
    from livingworld.personality import dialogue as D
    from livingworld.social.traits import traits_of
    entry = P.entry_for(db, settings, npc_id)
    out["dialogue"] = D.to_json(D.resolve(p, traits_of(db, npc_id), entry.dialogue if entry else None,
                                          settings.dialogue_enabled))
    from livingworld.personality import intimacy as I
    out["intimacy"] = I.to_json(I.resolve(db, p, entry.intimacy if entry else None, settings.intimacy_enabled))
    if player_id is not None:
        g = discovery.gate(db, p, player_id)
        out["player"] = {"willing_to_share": g.willing, "tiers": sorted(g.tiers), "why": g.why,
                         "knows": discovery.known(db, npc_id, player_id)}
    return out


def player_view(db, settings: Settings, npc_id: int, player_id: int, now: float) -> dict[str, Any]:
    """Only what the player has found out (for a player-facing view): never the profile itself."""
    p = P.resolve(db, npc_id, settings, now)
    if not p.active:
        return {"name": p.name, "knows": {}}
    knows = discovery.known(db, npc_id, player_id)
    out = {}
    for item, state in knows.items():
        key, _, value = item.partition(":")
        attr = schema.BY_KEY.get(key)
        if attr is None:
            continue
        shown = attr.words.get(value, value) if value else (
            schema.describe_number(p.values[key]) if attr.kind == "number" else attr.words.get(p.values.get(key), ""))
        out[item] = {"state": state, "what": f"{attr.label}: {shown}"}
    return {"name": p.name, "knows": out}


def export(db, settings: Settings, npc_ids: list[int], now: float) -> dict[str, Any]:
    """A JSON document with each profile's effective values, mode and hand-set values (importable)."""
    profiles = {}
    for npc_id in npc_ids:
        p = P.resolve(db, npc_id, settings, now)
        if not p.eligible:
            continue
        profiles[key_for(db, npc_id)] = {"name": p.name, "enabled": p.enabled, "mode": p.mode,
                                         "values": p.values, "manual": p.manual, "private": sorted(p.private)}
    return {"version": schema.PROFILE_VERSION, "kind": "livingworld-personality-export", "profiles": profiles}


def import_profiles(sfile: SettingsFile, data: dict[str, Any], db=None, take: str = "manual") -> list[str]:
    """Write an export (or a settings-shaped {"npcs": ...}) into the settings file as hand-set values.
    ``take='manual'`` imports only what was set by hand in the export; ``'values'`` locks every effective value.
    Returns the problems found (bad entries are skipped)."""
    s = sfile.get()
    problems: list[str] = []
    entries = data.get("profiles") or data.get("npcs") or {}
    for key, raw in entries.items():
        if not isinstance(raw, dict):
            problems.append(f"{key}: not an object")
            continue
        chosen = raw.get(take) if take in raw else raw.get("values") or {}
        entry = s.npcs.get(key) or NpcSettings(key=key)
        for attr, value in (chosen or {}).items():
            try:
                entry.values[attr] = schema.validate_value(attr, value)
            except ValueError as e:
                problems.append(f"{key}: {e}")
        if raw.get("mode") in schema.MODES:
            entry.mode = raw["mode"]
        if isinstance(raw.get("enabled"), bool):
            entry.enabled = raw["enabled"]
        for item in raw.get("private") or []:
            if str(item).split(":")[0] in schema.BY_KEY and item not in entry.private:
                entry.private.append(item)
        s.npcs[key] = entry
        if db is not None:
            for npc_id in find(db, key):
                audit(db, npc_id, "import", {"values": list((chosen or {}).keys()), "mode": entry.mode})
    sfile.save(s)
    return problems


def set_value(sfile: SettingsFile, key: str, attr: str, value: Any) -> None:
    s = sfile.get()
    entry = s.npcs.get(key) or NpcSettings(key=key)
    entry.values[attr] = schema.validate_value(attr, value)
    s.npcs[key] = entry
    sfile.save(s)


def set_enabled(sfile: SettingsFile, key: str, enabled: bool, db=None, npc_id: int | None = None) -> None:
    s = sfile.get()
    entry = s.npcs.get(key) or NpcSettings(key=key)
    entry.enabled = enabled
    s.npcs[key] = entry
    sfile.save(s)
    if db is not None:
        audit(db, npc_id, "enable" if enabled else "disable", {"key": key})


def reset(db, sfile: SettingsFile | None, npc_id: int, now: float, clear_manual: bool = False) -> None:
    """Back to her generated starting point: change over time is dropped (on this timeline; an older save that
    still has the changes keeps them). ``clear_manual`` also removes her hand-set values from the settings file."""
    P.add_change(db, npc_id, "*", "reset", "reset to generated defaults by the administrator", now)
    if clear_manual and sfile is not None:
        s = sfile.get()
        name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]
        for key in [k for k in s.npcs if k == key_for(db, npc_id) or k.lower() == name.lower()]:
            s.npcs[key].values = {}
        sfile.save(s)
    audit(db, npc_id, "reset", {"clear_manual": clear_manual})


def regenerate(db, npc_id: int, now: float) -> dict[str, Any]:
    """Generate her starting point again (after a generator update or a bio change) and drop change over time."""
    before = db.execute("SELECT generated_json FROM personality_profile WHERE npc_id = ?", (npc_id,)).fetchone()
    gen = generate.store_generated(db, npc_id, now, replace=True)
    P.add_change(db, npc_id, "*", "reset", "starting point generated again by the administrator", now)
    audit(db, npc_id, "regenerate", {"before": json.loads(before[0]) if before else None})
    return gen.values


# ---- dialogue personality editor (settings file only; takes effect at the next line, no regeneration) ----

def _dialogue_entry(sfile: SettingsFile, key: str):
    s = sfile.get()
    entry = s.npcs.get(key) or NpcSettings(key=key)
    s.npcs[key] = entry
    return s, entry


def set_dialogue(sfile: SettingsFile, key: str, part: str, name: str, value: Any) -> None:
    """part 'toggles' (value bool), 'sliders' (0..100) or 'enabled' (bool, name ignored)."""
    from livingworld.personality import dialogue as D
    s, entry = _dialogue_entry(sfile, key)
    raw = dict(entry.dialogue)
    if part == "enabled":
        raw["enabled"] = bool(value)
    else:
        raw.setdefault(part, {})
        raw[part] = dict(raw[part], **{name: value})
    clean, problems = D.validate(raw)
    if problems:
        raise ValueError("; ".join(problems))
    entry.dialogue = clean
    sfile.save(s)


def phrase(sfile: SettingsFile, key: str, action: str, category: str, text: str) -> None:
    """action 'add' or 'remove' one phrase in a category (no duplicates; removing a generated one hides it)."""
    from livingworld.personality import dialogue as D
    if category not in D.CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(D.CATEGORIES)}")
    s, entry = _dialogue_entry(sfile, key)
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in entry.dialogue.items()}
    norm = D._norm(text)
    added = [x for x in raw.get("vocabulary", {}).get(category, []) if D._norm(x) != norm]
    removed = [x for x in raw.get("remove", {}).get(category, []) if D._norm(x) != norm]
    if action == "add":
        added.append(text)
    elif action == "remove":
        removed.append(text)
    else:
        raise ValueError("action must be add or remove")
    raw.setdefault("vocabulary", {})[category] = added
    raw.setdefault("remove", {})[category] = removed
    raw["vocabulary"] = {c: v for c, v in raw["vocabulary"].items() if v}
    raw["remove"] = {c: v for c, v in raw["remove"].items() if v}
    entry.dialogue, _ = D.validate(raw)
    sfile.save(s)


def reset_dialogue(sfile: SettingsFile, key: str) -> None:
    s = sfile.get()
    if key in s.npcs:
        s.npcs[key].dialogue = {}
        sfile.save(s)


def set_rating(sfile: SettingsFile, key: str, group: str, rating: str | None) -> None:
    """Set one intimacy rating by hand (None removes the hand setting). 'boundary' adds a limit; any other
    rating set by hand is the only thing that lifts one."""
    from livingworld.personality import intimacy as I
    s = sfile.get()
    entry = s.npcs.get(key) or NpcSettings(key=key)
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in entry.intimacy.items()}
    ratings = raw.setdefault("ratings", {})
    if rating is None:
        ratings.pop(group, None)
    else:
        ratings[group] = rating
    clean, problems = I.validate(raw)
    if problems:
        raise ValueError("; ".join(problems))
    entry.intimacy = clean
    s.npcs[key] = entry
    sfile.save(s)


def set_category(sfile: SettingsFile, key: str, group: str, on: bool) -> None:
    from livingworld.personality import intimacy as I
    s = sfile.get()
    entry = s.npcs.get(key) or NpcSettings(key=key)
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in entry.intimacy.items()}
    raw.setdefault("categories", {})[group] = on
    clean, problems = I.validate(raw)
    if problems:
        raise ValueError("; ".join(problems))
    entry.intimacy = clean
    s.npcs[key] = entry
    sfile.save(s)
