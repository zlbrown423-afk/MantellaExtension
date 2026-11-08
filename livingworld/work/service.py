"""What the integration calls: keep tasks up to date, add task lines to prompts, react to the task
actions, and close offers nobody took up. Every method opens its own transaction."""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.social import claims, processor
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore
from livingworld.work import conditions as C, consequences, needs, prompts, tasks as T
from livingworld.work.templates import by_key

AGREE = "mantella_lw_task_agree"
CHECK = "mantella_lw_task_check"
RELEASE = "mantella_lw_task_release"
ACTIONS = (AGREE, CHECK, RELEASE)


@dataclass
class Scene:
    """What the prompt builder knows about the moment."""
    now: float
    hour: int
    weather: str | None
    player_id: int | None
    player_name: str
    present: list[int]


class Work:
    def __init__(self, store: WorldStore) -> None:
        self.store = store
        self._offered: set[int] = set()   # tasks offered in the conversation now running

    # ---- upkeep ----
    def refresh(self, now: float, hour: int | None = None, weather: str | None = None) -> needs.Upkeep:
        with self.store.transaction() as db:
            result = needs.upkeep(db, now, hour, weather)
            processor.process(db, {"appraisal": claims.appraise})   # outcomes become news at once
        return result

    # ---- prompts ----
    def lines_for(self, npc_id: int, name: str, scene: Scene) -> list[str]:
        if scene.player_id is None or is_child_safe(self.store, npc_id):
            return []
        with self.store.transaction() as db:
            return self._lines(db, npc_id, name, scene)

    def _lines(self, db, npc_id: int, name: str, scene: Scene) -> list[str]:
        out = []
        player = scene.player_id
        for task in T.open_for_giver(db, npc_id):
            t = by_key(task.template)
            if t is None:
                continue
            if task.state in T.TAKEN and task.assignee_id == player:
                out.append(prompts.status(name, scene.player_name, task, scene.now,
                                          C.pause_reason(t, scene.hour, scene.weather)))
            elif task.state in ("available", "offered"):
                checks = needs.offer_checks(db, task, t, player, scene.now, scene.hour, scene.weather)
                if all(c.ok for c in checks):
                    if task.state == "available":
                        T.change(db, task, "offered", "offered in conversation", scene.now,
                                 witnesses=[i for i in scene.present if i != player])
                    self._offered.add(task.task_id)
                    out.append(prompts.offer(name, scene.player_name, task, t))
                elif any(c.name == "reliability" and not c.ok for c in checks):
                    _, done, let_down = C.reliability(db, player)
                    out.append(prompts.unreliable(name, scene.player_name, done, let_down))
        for task in T.find(db, "giver_id = ? AND assignee_id = ? AND state IN ('completed','failed','expired')"
                               " AND state_gt >= ?", (npc_id, player, scene.now - prompts.RECENT_OUTCOME_DAYS)):
            out.append(prompts.outcome(db, name, scene.player_name, task, scene.now))
        return out

    # ---- actions the NPC takes in conversation ----
    def action(self, identifier: str, npc_id: int, scene: Scene) -> str | None:
        """Handle a task action by ``npc_id`` (the giver). Returns what happened, for the log."""
        if identifier not in ACTIONS or scene.player_id is None:
            return None
        with self.store.transaction() as db:
            witnesses = list(scene.present)
            if identifier == AGREE:
                task = next((x for x in T.open_for_giver(db, npc_id) if x.state in ("available", "offered")), None)
                if task is None:
                    return "nothing to agree to"
                t = by_key(task.template)
                deadline = scene.now + t.deadline_days if t and t.deadline_days else None
                task = T.change(db, task, "accepted", "agreed in conversation", scene.now,
                                assignee_id=scene.player_id, deadline_gt=deadline, witnesses=witnesses)
                db.execute("INSERT INTO obligation (timeline_id, kind, debtor_id, creditor_id, description, due_gt,"
                           " status, created_gt) VALUES (?, 'promise', ?, ?, ?, ?, 'open', ?)",
                           (T._timeline(db), scene.player_id, npc_id,
                            f"agreed to help {task.title} (task {task.task_id})", deadline, scene.now))
                self._offered.discard(task.task_id)
                return f"task {task.task_id} accepted"
            task = next((x for x in T.open_for_giver(db, npc_id)
                         if x.state in T.TAKEN and x.assignee_id == scene.player_id), None)
            if task is None:
                return "no task in hand"
            if identifier == CHECK:
                T.change(db, task, None, f"checked: {task.progress:g} of {task.need_count}", scene.now)
                if task.done_enough:
                    consequences.completed(db, task, scene.now, "checked and found done")
                    return f"task {task.task_id} completed"
                return f"task {task.task_id} checked ({task.progress:g}/{task.need_count})"
            consequences.let_down(db, task, "failed", scene.now, "released: player said they cannot finish",
                                  withdrew=True)
            return f"task {task.task_id} released"

    def mood(self, npc_id: int, score: int, scene: Scene) -> None:
        """The giver's mood tag while the worker is with them counts toward how the work went."""
        if scene.player_id is None or scene.player_id not in scene.present:
            return
        with self.store.transaction() as db:
            for task in T.open_for_giver(db, npc_id):
                if task.state in T.TAKEN and task.assignee_id == scene.player_id:
                    T.change(db, task, None, "mood while the work was on", scene.now, mood=int(score))

    def conversation_closed(self, now: float) -> list[int]:
        """Offers made in this conversation that were not taken up count as a polite no."""
        declined = []
        with self.store.transaction() as db:
            for task_id in sorted(self._offered):
                task = T.get(db, task_id)
                if task is not None and task.state == "offered":
                    T.change(db, task, "available", "declined (or not taken up) in conversation", now)
                    declined.append(task_id)
        self._offered.clear()
        return declined

    def wants_a_word(self, scene: Scene) -> list[int]:
        """Givers with work they would offer the player right now (for the game's approach step, W2)."""
        if scene.player_id is None:
            return []
        out = []
        with self.store.transaction() as db:
            for task in T.find(db, "state = 'available'"):
                t = by_key(task.template)
                if t and all(c.ok for c in needs.offer_checks(db, task, t, scene.player_id, scene.now,
                                                               scene.hour, scene.weather)):
                    out.append(task.giver_id)
        return out


def is_child_safe(store: WorldStore, npc_id: int) -> bool:
    with store.reading() as db:
        return is_child(db, npc_id)
