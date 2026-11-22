"""The paper coin ledger: purses, balances and transfers (append-only, on the save timeline)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from livingworld.civic.institutions import active_timeline

KINDS = ("stipend", "allowance", "upkeep", "wage", "fee", "build", "gift", "fine", "tribute", "loan", "repayment",
         "opening", "adjustment")


def purse_of(inst_key: str) -> str:
    return f"inst:{inst_key}"


def npc_purse(npc_id: int) -> str:
    return f"npc:{npc_id}"


@dataclass(frozen=True)
class Entry:
    entry_id: int
    purse: str
    amount: int
    kind: str
    counterparty: str | None
    memo: str
    game_time: float


def balance(db, purse: str, at_gt: float | None = None) -> int:
    if at_gt is None:
        r = db.execute("SELECT COALESCE(SUM(amount), 0) FROM v_ledger_entry WHERE purse = ?", (purse,)).fetchone()
    else:
        r = db.execute("SELECT COALESCE(SUM(amount), 0) FROM v_ledger_entry WHERE purse = ? AND game_time <= ?",
                       (purse, at_gt)).fetchone()
    return int(r[0])


def entries(db, purse: str, since_gt: float | None = None, until_gt: float | None = None) -> list[Entry]:
    sql = "SELECT entry_id, purse, amount, kind, counterparty, memo, game_time FROM v_ledger_entry WHERE purse = ?"
    params: list = [purse]
    if since_gt is not None:
        sql += " AND game_time > ?"
        params.append(since_gt)
    if until_gt is not None:
        sql += " AND game_time <= ?"
        params.append(until_gt)
    return [Entry(*tuple(r)) for r in db.execute(sql + " ORDER BY game_time, entry_id", params)]


def record(db, purse: str, amount: int, kind: str, gt: float, counterparty: str | None = None, memo: str = "",
           event_id: int | None = None, transfer_key: str | None = None) -> int:
    """One entry on one purse. Use :func:`transfer` when the money comes from another purse."""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind!r}")
    if int(amount) == 0:
        return 0
    cur = db.execute("INSERT INTO ledger_entry (timeline_id, purse, amount, kind, counterparty, transfer_key, memo,"
                     " cause_event_id, game_time) VALUES (?,?,?,?,?,?,?,?,?)",
                     (active_timeline(db), purse, int(amount), kind, counterparty, transfer_key, memo, event_id, gt))
    return cur.lastrowid


def transfer(db, source: str, target: str, amount: int, kind: str, gt: float, memo: str = "",
             event_id: int | None = None) -> str | None:
    """Move ``amount`` gold on paper from ``source`` to ``target``: two entries with one transfer key.
    A purse may go below zero (that is a debt or arrears, which the caller decides how to treat)."""
    amount = int(amount)
    if amount <= 0:
        return None
    key = uuid.uuid4().hex[:16]
    record(db, source, -amount, kind, gt, target, memo, event_id, key)
    record(db, target, amount, kind, gt, source, memo, event_id, key)
    return key
