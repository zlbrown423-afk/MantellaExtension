"""Command line administration for personality profiles. Run from the Mantella folder:

    python -m livingworld.personality --world <path to World.sqlite> list
    python -m livingworld.personality --world W.sqlite inspect Gerdur
    python -m livingworld.personality --world W.sqlite history Gerdur
    python -m livingworld.personality --world W.sqlite known Gerdur          (what the player has found out)
    python -m livingworld.personality --world W.sqlite export [Gerdur ...] > profiles.json
    python -m livingworld.personality --world W.sqlite import profiles.json [--take values]
    python -m livingworld.personality --world W.sqlite set Gerdur libido 70
    python -m livingworld.personality --world W.sqlite set Gerdur boundaries no_unasked_touch,not_with_taken
    python -m livingworld.personality --world W.sqlite mode Gerdur manual
    python -m livingworld.personality --world W.sqlite disable Gerdur  |  enable Gerdur
    python -m livingworld.personality --world W.sqlite reset Gerdur [--clear-manual]
    python -m livingworld.personality --world W.sqlite regenerate Gerdur
    python -m livingworld.personality schema > personality.schema.json

The settings file defaults to personality.json next to the world database (--settings to choose another).
Close Mantella first for reset / regenerate (they write to the world database).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from livingworld.personality import admin, schema
from livingworld.personality.config import NpcSettings, SettingsFile


def _value(attr: str, raw: str):
    a = schema.BY_KEY.get(attr)
    if a is None:
        raise SystemExit(f"unknown attribute {attr}; known: {', '.join(schema.BY_KEY)}")
    if a.kind == "number":
        return float(raw)
    if a.kind == "list":
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m livingworld.personality", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", help="the world database (.sqlite)")
    ap.add_argument("--settings", help="the settings file (default: personality.json next to the database)")
    ap.add_argument("command", choices=("list", "inspect", "history", "known", "export", "import", "set", "mode",
                                        "enable", "disable", "reset", "regenerate", "schema"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--take", default="manual", choices=("manual", "values"))
    ap.add_argument("--clear-manual", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "schema":
        print(json.dumps(schema.json_schema(), indent=2))
        return 0
    if not a.world:
        ap.error("--world is required")
    from livingworld.store.world_store import WorldStore
    store = WorldStore(a.world)
    sfile = SettingsFile(a.settings or os.path.join(os.path.dirname(os.path.abspath(a.world)), "personality.json"))
    settings = sfile.get()
    for p in settings.problems:
        print(f"settings: {p}", file=sys.stderr)
    now = store.scalar("SELECT COALESCE(MAX(game_time), 0) FROM world_event") or 0.0
    player = store.scalar("SELECT npc_id FROM npc WHERE is_player = 1")

    def one(name: str) -> int:
        ids = admin.find(store._conn, name)
        if not ids:
            raise SystemExit(f"nobody called {name!r} in this world")
        return ids[0]

    with store.transaction() as db:
        if a.command == "list":
            for r in db.execute("SELECT npc_id, display_name FROM npc WHERE is_player = 0 AND sex = 'female'"
                                " ORDER BY display_name"):
                from livingworld.personality.profile import resolve
                p = resolve(db, r[0], settings, now, create=False)
                print(f"{r[1]:<28} {admin.key_for(db, r[0]):<32} "
                      f"{('mode ' + p.mode) if p.active else ('off: ' + (p.why if not p.eligible else 'disabled'))}")
        elif a.command == "inspect":
            print(json.dumps(admin.inspect(db, settings, one(a.args[0]), now, player), indent=2, ensure_ascii=False))
        elif a.command == "history":
            print(json.dumps(admin.history(db, one(a.args[0])), indent=2, ensure_ascii=False))
        elif a.command == "known":
            print(json.dumps(admin.player_view(db, settings, one(a.args[0]), player, now), indent=2, ensure_ascii=False))
        elif a.command == "export":
            ids = [one(n) for n in a.args] or [r[0] for r in db.execute(
                "SELECT npc_id FROM npc WHERE is_player = 0 AND sex = 'female'")]
            print(json.dumps(admin.export(db, settings, ids, now), indent=2, ensure_ascii=False))
        elif a.command == "import":
            with open(a.args[0], encoding="utf-8-sig") as f:
                problems = admin.import_profiles(sfile, json.load(f), db, take=a.take)
            for p in problems:
                print(f"skipped: {p}", file=sys.stderr)
            print(f"imported into {sfile.path}")
        elif a.command == "set":
            npc = one(a.args[0])
            admin.set_value(sfile, admin.key_for(db, npc), a.args[1], _value(a.args[1], a.args[2]))
            admin.audit(db, npc, "set", {"attribute": a.args[1], "value": a.args[2]})
            print(f"{a.args[0]}: {a.args[1]} set by hand (locked)")
        elif a.command == "mode":
            npc = one(a.args[0])
            if a.args[1] not in schema.MODES:
                raise SystemExit(f"mode must be one of {', '.join(schema.MODES)}")
            s = sfile.get()
            key = admin.key_for(db, npc)
            entry = s.npcs.get(key) or NpcSettings(key=key)
            entry.mode = a.args[1]
            s.npcs[key] = entry
            sfile.save(s)
            admin.audit(db, npc, "mode", {"mode": a.args[1]})
        elif a.command in ("enable", "disable"):
            npc = one(a.args[0])
            admin.set_enabled(sfile, admin.key_for(db, npc), a.command == "enable", db, npc)
        elif a.command == "reset":
            admin.reset(db, sfile, one(a.args[0]), now, clear_manual=a.clear_manual)
        elif a.command == "regenerate":
            print(json.dumps(admin.regenerate(db, one(a.args[0]), now), indent=2))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
