"""A model of the game side of body language (the BL1 Papyrus in Mantella-Spell's MantellaRepository.psc),
for tests that check reactions settle instead of feeding each other. It mirrors the same rules: hysteresis
on the comfort distance, cooldowns, a cap on reactions per minute, "backed off" only when the *player*
moved away, and NPCs never stepping toward anyone. It is a model, not the Papyrus itself: keep the two in
step when either changes.

Positions are on a line in metres; time is in real seconds; one tick is 0.5 s like the game's loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TICK_S = 0.5
EXIT_FACTOR = 1.3          # relax only this far beyond the comfort distance
STEP_TO_FACTOR = 1.35      # a step back ends just beyond the exit distance
REPORT_AFTER_S = 1.5       # inside this long before it counts as crowding
STEP_COOLDOWN_S = 10.0
ARMS_COOLDOWN_S = 20.0
ARMS_HOLD_S = 6.0
MAX_REACTIONS_PER_MIN = 4
PLAYER_MOVED_M = 0.3       # "backed off" needs the player to have moved at least this far
CROWD_MEMORY_S = 180.0


@dataclass
class SimNpc:
    name: str
    x: float
    comfort: float
    guard: float = 0.3
    steps_back: bool = True
    inside: bool = False
    inside_since: float = 0.0
    reported: bool = False
    min_dist: float = 99.0
    crowd_n: int = 0
    last_crowd_at: float = -1e9
    last_step_at: float = -1e9
    last_arms_at: float = -1e9
    arms_off_at: float = 0.0
    player_x_at_entry: float = 0.0
    reactions: list[float] = field(default_factory=list)
    records: list[tuple[float, str]] = field(default_factory=list)
    steps: int = 0
    arms: int = 0

    def can_react(self, now: float) -> bool:
        self.reactions = [t for t in self.reactions if now - t < 60.0]
        return len(self.reactions) < MAX_REACTIONS_PER_MIN


def tick(now: float, player_x: float, npcs: list[SimNpc]) -> None:
    for npc in npcs:
        d = abs(npc.x - player_x)
        if npc.arms_off_at and now >= npc.arms_off_at:
            npc.arms_off_at = 0.0
        if not npc.inside and d < npc.comfort:
            npc.inside, npc.inside_since, npc.reported = True, now, False
            npc.min_dist, npc.player_x_at_entry = d, player_x
        elif npc.inside:
            npc.min_dist = min(npc.min_dist, d)
            if d > npc.comfort * EXIT_FACTOR:
                npc.inside = False
                if npc.reported and abs(player_x - npc.player_x_at_entry) >= PLAYER_MOVED_M:
                    npc.records.append((now, "backed_off"))
                continue
            if not npc.reported and now - npc.inside_since >= REPORT_AFTER_S:
                npc.crowd_n = npc.crowd_n + 1 if now - npc.last_crowd_at < CROWD_MEMORY_S else 1
                npc.last_crowd_at = now
                npc.records.append((now, "crowd"))
                npc.reported = True
            if npc.reported and npc.steps_back and now - npc.last_step_at >= STEP_COOLDOWN_S and npc.can_react(now):
                away = 1.0 if npc.x >= player_x else -1.0          # always away from the player, never toward
                npc.x = player_x + away * npc.comfort * STEP_TO_FACTOR
                npc.last_step_at = now
                npc.reactions.append(now)
                npc.steps += 1
            if npc.reported and npc.guard >= 0.6 and not npc.arms_off_at \
                    and now - npc.last_arms_at >= ARMS_COOLDOWN_S and npc.can_react(now):
                npc.arms_off_at, npc.last_arms_at = now + ARMS_HOLD_S, now
                npc.reactions.append(now)
                npc.arms += 1


def run(npcs: list[SimNpc], player_path, seconds: float) -> None:
    """``player_path(t)`` gives the player's position at time t."""
    t = 0.0
    while t <= seconds:
        tick(t, player_path(t), npcs)
        t += TICK_S
