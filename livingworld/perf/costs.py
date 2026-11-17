"""What the AI calls cost: a summary of the ``llm_request`` rows that AI cost logging writes.

    python -m livingworld.perf.costs <world.sqlite | data\\livingworld folder> [--days 7] [--top 10]

Reads the world database read-only, so it is safe while the game and the Mantella server run. A folder
means every world database in it. It counts every row, whatever save (timeline) the call was made on:
the money was spent either way.

Sections: total, foreground (the player waits: dialogue, tool calls, vision) against background
(summaries, memory extraction, diaries), by model, by task, by day, the NPCs whose replies cost most, and
the calls with no known price (their tokens are counted; add a price with ``MANTELLA_LW_PRICES``, see
``llm_usage.py``). Rows written before cost logging (no tokens, no lane) are counted apart.
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any

LOGGED = "lane IS NOT NULL"   # rows written by cost logging (schema 024 on)


@dataclass
class Line:
    label: str
    calls: int
    prompt: int
    completion: int
    cached: int
    cost: float


def connect(path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    return db


def has_cost_columns(db: sqlite3.Connection) -> bool:
    cols = {r[1] for r in db.execute("PRAGMA table_info(llm_request)")}
    return "lane" in cols


def _where(days: float | None) -> tuple[str, tuple[Any, ...]]:
    if days is None:
        return LOGGED, ()
    return f"{LOGGED} AND at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{float(days)} days",)


SUMS = ("COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt, COALESCE(SUM(completion_tokens), 0)"
        " AS completion, COALESCE(SUM(cached_tokens), 0) AS cached, COALESCE(SUM(cost_usd), 0) AS cost")


def grouped(db: sqlite3.Connection, key: str, days: float | None, order: str = "cost DESC",
            limit: int | None = None, join: str = "") -> list[Line]:
    where, args = _where(days)
    sql = (f"SELECT {key} AS label, {SUMS} FROM llm_request {join} WHERE {where} GROUP BY {key}"
           f" ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else ""))
    return [Line(str(r["label"] if r["label"] is not None else "-"), r["calls"], r["prompt"], r["completion"],
                 r["cached"], r["cost"]) for r in db.execute(sql, args)]


def summarize(db: sqlite3.Connection, days: float | None = None, top: int = 10) -> dict[str, Any]:
    """Everything the report prints, as data (used by the tests)."""
    if not has_cost_columns(db):
        return {"schema_ready": False}
    where, args = _where(days)
    total = grouped(db, "'total'", days)
    unknown = db.execute(
        f"SELECT model, COUNT(*) AS calls, COALESCE(SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)),0)"
        f" AS tokens FROM llm_request WHERE {where} AND cost_source IN ('unknown', 'mixed')"
        f" GROUP BY model ORDER BY tokens DESC", args).fetchall()
    estimated = db.execute(f"SELECT COUNT(*) FROM llm_request WHERE {where} AND estimated = 1", args).fetchone()[0]
    errors = db.execute(f"SELECT COUNT(*) FROM llm_request WHERE {where} AND status != 'ok'", args).fetchone()[0]
    before = db.execute("SELECT COUNT(*) FROM llm_request WHERE lane IS NULL").fetchone()[0]
    return {
        "schema_ready": True,
        "total": total[0] if total and total[0].calls else Line("total", 0, 0, 0, 0, 0.0),
        "lanes": grouped(db, "lane", days),
        "models": grouped(db, "model", days),
        "tasks": grouped(db, "task", days),
        "days": grouped(db, "substr(at, 1, 10)", days, order="label DESC", limit=14),
        "npcs": grouped(db, "npc.display_name", days, limit=top,
                        join="JOIN npc ON npc.npc_id = llm_request.npc_id"),
        "unknown": [(r["model"], r["calls"], r["tokens"]) for r in unknown],
        "estimated": estimated,
        "errors": errors,
        "before_logging": before,
    }


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _table(title: str, lines: list[Line]) -> list[str]:
    if not lines:
        return []
    width = max(12, min(48, max(len(l.label) for l in lines)))
    out = [title]
    for l in lines:
        out.append(f"  {l.label[:width]:<{width}}  ${l.cost:>9.4f}  {l.calls:>6} calls  in {_tokens(l.prompt):>7}"
                   f"  out {_tokens(l.completion):>7}" + (f"  cached {_tokens(l.cached)}" if l.cached else ""))
    return out + [""]


def render(name: str, s: dict[str, Any], days: float | None) -> str:
    span = f"last {days:g} days" if days is not None else "all time"
    out = [f"AI cost: {name} ({span})", ""]
    if not s.get("schema_ready"):
        return "\n".join(out + ["  This database has no cost columns yet: start the server with this build once",
                                "  (schema 024) and play; calls are logged from then on.", ""])
    t: Line = s["total"]
    out.append(f"Total: ${t.cost:.4f} in {t.calls} calls, {_tokens(t.prompt + t.completion)} tokens"
               f" (in {_tokens(t.prompt)}, out {_tokens(t.completion)}"
               + (f", {_tokens(t.cached)} of the input from the provider's cache" if t.cached else "") + ")")
    if t.calls:
        out.append(f"Average: ${t.cost / t.calls:.5f} per call")
    out.append("")
    out += _table("Foreground (player waits) / background:", s["lanes"])
    out += _table("By model:", s["models"])
    out += _table("By task:", s["tasks"])
    out += _table("By day (UTC):", s["days"])
    out += _table("NPCs whose calls cost most:", s["npcs"])
    if s["unknown"]:
        out.append("No known price (stored as $0; set MANTELLA_LW_PRICES or use OpenRouter, which reports cost):")
        out += [f"  {m}: {c} calls, {_tokens(n)} tokens" for m, c, n in s["unknown"]]
        out.append("")
    notes = []
    if s["estimated"]:
        notes.append(f"{s['estimated']} call(s) had tokens counted locally (the service sent no usage)")
    if s["errors"]:
        notes.append(f"{s['errors']} call(s) failed or were rate limited")
    if s["before_logging"]:
        notes.append(f"{s['before_logging']} older row(s) from before cost logging are not counted")
    out += [f"Note: {n}." for n in notes]
    return "\n".join(out).rstrip() + "\n"


def databases(path: str) -> list[str]:
    if os.path.isdir(path):
        return sorted(p for p in glob.glob(os.path.join(path, "*.sqlite")))
    return [path]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize what the AI calls cost (Living World cost logging).")
    ap.add_argument("path", help="a world .sqlite file, or the data\\livingworld folder for every world")
    ap.add_argument("--days", type=float, default=None, help="only the last N days (default: everything)")
    ap.add_argument("--top", type=int, default=10, help="how many NPCs to list (default 10)")
    args = ap.parse_args(argv)
    paths = databases(args.path)
    if not paths:
        print(f"No world databases in {args.path}", file=sys.stderr)
        return 1
    for path in paths:
        try:
            db = connect(path)
        except sqlite3.Error as e:
            print(f"{path}: cannot open ({e})", file=sys.stderr)
            continue
        try:
            print(render(os.path.basename(path), summarize(db, args.days, args.top), args.days))
        except sqlite3.Error as e:
            print(f"{path}: cannot read ({e})", file=sys.stderr)
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
