"""The duty in hand, kept in front of the player until the shift is over.

The game shows it as a journal quest, "Guard Duty" (the small ``LWGuardDuty.esp`` plugin): its objective is
the duty and when the shift ends ("Stand watch at the main gate until 2:00 pm"). The objective pops up at the
top of the view when it is given and again every ``MANTELLA_LW_DUTY_HUD_EVERY`` real seconds (default 180;
0 = only when it changes), with the time left. When the duty can be reported a second objective appears,
"Report back to Caius", with a compass marker on them. The game keeps the clock itself between
conversations: at the shift's end the duty objective is ticked off (or failed, if the player was never seen at
the post) and the report one turns into "Shift over: report back to Caius by 2:00 am"; past that deadline it
fails and the quest closes. When the server sees the duty settled (reported, missed or dropped), it sends
task 0 with how it went and the game closes the quest the same way.

The server sends this with an NPC line whenever it changes (and once per conversation, so a save loaded in
between is put right). The texts are made here; the game only adds the clock and the time left.

Off with ``MANTELLA_LW_DUTY_HUD=0`` (the game then closes any duty it shows).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from livingworld.guard import duty as DU, orgs as O, store as S
from livingworld.work import tasks as T

ENV = "MANTELLA_LW_DUTY_HUD"
ENV_EVERY = "MANTELLA_LW_DUTY_HUD_EVERY"
DEFAULT_EVERY = 180.0


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def every_seconds() -> float:
    """How often (real seconds) the game shows the objective again; 0 = only when it changes."""
    try:
        return max(0.0, float(os.environ.get(ENV_EVERY, DEFAULT_EVERY)))
    except ValueError:
        return DEFAULT_EVERY


@dataclass(frozen=True)
class Objective:
    task_id: int                 # 0 = no duty in hand: close the quest
    text: str = ""               # the duty line, without the clock ("Stand watch at the main gate")
    end: float = 0.0             # game days (+1, like the records) the shift ends; 0 = no shift (done when done)
    ready_at: float = -1.0       # when it may be reported; -1 = not yet (still to be seen at the post / dealt with)
    report_by: float = 0.0       # the report deadline (missed after this); 0 = none
    seen: bool = False           # seen at the post / dealt with
    boss: str = ""               # whom to report to
    boss_ref: int = 0            # their reference, plugin-relative (0 = unknown: no compass marker)
    boss_plugin: str = ""
    outcome: str = ""            # with task 0: 'done' | 'failed' (how the last duty ended), else ''
    last_task: int = 0           # with task 0: the duty that outcome is about

    def signature(self) -> tuple:
        return (self.task_id, self.text, round(self.end, 5), round(self.ready_at, 5), round(self.report_by, 5),
                self.seen, self.boss, self.boss_ref, self.outcome, self.last_task)


NONE = Objective(0)


def _cap(text: str) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:] if text else text


def _boss_ref(db, npc_id: int | None) -> tuple[int, str]:
    """The superior's placed reference as (plugin-relative id, plugin), for the compass marker; (0, '') if unknown."""
    if npc_id is None:
        return 0, ""
    row = db.execute("SELECT ref_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    raw = (row[0] if row else None) or ""
    plugin, _, local = raw.rpartition("|")      # 'Skyrim.esm|0x00038258'
    if not plugin or ":" in plugin:             # 'load_index:05' / 'light:FE1': no stable plugin name
        return 0, ""
    try:
        return int(local, 16) & 0x00FFFFFF, plugin
    except ValueError:
        return 0, ""


def _settled(db, member_id: int) -> Objective:
    """No duty in hand: how the last one ended, so the game can tick it off or fail it."""
    last = DU.last(db, member_id)
    if last is None or last.state not in T.DONE:
        return NONE
    return Objective(0, outcome="done" if last.state == "completed" else "failed", last_task=last.task_id)


def objective(db, player_id: int | None, now: float) -> Objective | None:
    """What the game should show for the player's duty now (None: the player is not in a guard)."""
    if player_id is None:
        return None
    svc = S.current(db, player_id)
    org = O.by_key(svc.org_key) if svc else None
    if svc is None or org is None:
        return None
    task = DU.in_hand(db, svc.member_id)
    d = DU.duty_for(db, task) if task is not None else None
    if task is None or d is None:
        return _settled(db, svc.member_id)
    boss_id = DU.superior_id(db, org, svc)
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (boss_id,)).fetchone() if boss_id else None
    boss = row[0] if row else (org.superior(svc.rank_key).name if org.superior(svc.rank_key) else "your superior")
    ref, plugin = _boss_ref(db, boss_id)
    done = DU.done_count(db, task, d) > 0
    if d.training:
        from livingworld.guard import training as TR
        n, total = TR.progress(db, org, svc)
        text = f"Training {n + 1}/{total}: {d.title}"
    elif d.incident_id is not None:
        text = f"Trouble: {d.title}"
    else:
        text = _cap(d.title)
    no_shift = DU._no_shift(d)
    end = 0.0 if no_shift else DU.shift_end(db, task, d)
    if not done:
        ready_at = -1.0
    elif no_shift:
        ready_at = 0.0
    else:
        ready_at = DU.assigned_gt(db, task) + d.shift_hours / 48.0
    return Objective(task.task_id, text, end, ready_at, task.deadline_gt or 0.0, done, boss, ref, plugin)


def objective_for(store, player_id: int | None, now: float) -> Objective | None:
    with store.reading() as db:
        return objective(db, player_id, now)
