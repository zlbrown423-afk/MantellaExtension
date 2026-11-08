"""The lines a task adds to an NPC's prompt. Plain facts about the need and how the work stands; the
NPC's own personality decides how to say it. Nothing here tells the NPC what the player will answer."""
from __future__ import annotations

from livingworld.memory.recall import time_ago
from livingworld.work import consequences, tasks as T
from livingworld.work.templates import Template

RECENT_OUTCOME_DAYS = 3.0


def _due(task: T.Task, now: float) -> str:
    if task.deadline_gt is None:
        return ""
    left = task.deadline_gt - now
    if left <= 0:
        return " The time you gave for it has run out."
    if left < 1:
        return f" You want it done within about {max(1, round(left * 24))} hours."
    return f" You want it done within about {round(left)} day{'s' if round(left) != 1 else ''}."


def _amount(task: T.Task) -> str:
    return f"{task.need_count} {task.units}" if task.need_count > 1 else task.units


def offer(name: str, player: str, task: T.Task, t: Template) -> str:
    pay = (f" You mean to pay {task.reward_gold} gold for it when it is done." if task.reward_gold
           else " You cannot pay for it; it would be a favour.")
    deadline = (f" It should be done within about {t.deadline_days:g} day{'s' if t.deadline_days != 1 else ''}."
                if t.deadline_days else "")
    return (f"{name} has work that genuinely needs doing: {t.why}. {name} needs someone to {task.title} "
            f"({_amount(task)}).{deadline}{pay} If it fits the moment, {name} may ask {player} for help and explain "
            f"why; {player} is free to say no, and a refusal is fine. If asked how: {t.instructions}")


def status(name: str, player: str, task: T.Task, now: float, pause: str | None) -> str:
    done = int(task.progress) if float(task.progress).is_integer() else round(task.progress, 1)
    line = (f"{player} agreed to help {name} {task.title}. So far {done} of {_amount(task)} are done."
            f"{_due(task, now)}")
    if task.state == "paused" and pause:
        line += f" The work is on hold for now: {pause}."
    if task.reward_gold:
        line += f" {name} promised {task.reward_gold} gold when it is finished."
    return line


def outcome(db, name: str, player: str, task: T.Task, now: float) -> str:
    ago = time_ago(now - (task.state_gt or now))
    if task.state == "completed":
        how = consequences.conduct(db, task, task.assignee_id, task.state_gt or now).words()
        pay = f" {name} promised {task.reward_gold} gold for it." if task.reward_gold else ""
        return (f"{player} finished helping {name} {task.title} {ago}"
                + (f" ({', '.join(how)})" if how else "") + f".{pay} {name} has not forgotten it.")
    if task.state == "failed" and T.count_changes(db, task.task_id, "said they cannot") + \
            T.count_changes(db, task.task_id, "released") > 0:
        return f"{player} agreed to help {name} {task.title} but backed out {ago}."
    return f"{player} agreed to help {name} {task.title} but never finished it ({ago})."


def unreliable(name: str, player: str, done: int, let_down: int) -> str:
    return (f"{name} no longer asks {player} for help with work: {player} has let people down {let_down} "
            f"time{'s' if let_down != 1 else ''} and finished only {done}. {name} may say so if it comes up.")
