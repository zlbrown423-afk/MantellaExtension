"""Body language for one world: takes the game's presence records, keeps each NPC's feelings, and hands
the dialogue model and the game what they need. Called from ``livingworld/integration.py``."""
from __future__ import annotations

from dataclasses import dataclass, field

from src import utils

from livingworld.bodylang import presence as P
from livingworld.bodylang import state
from livingworld.perception.records import BadRecord
from livingworld.social.traits import traits_of
from livingworld.store.identity import IdentityRegistry
from livingworld.store.minors import is_child
from livingworld.store.world_store import WorldStore

logger = utils.get_logger()


@dataclass
class Noticed:
    game_time: float
    text: str


@dataclass
class BodyLanguage:
    store: WorldStore
    _seen_keys: set[str] = field(default_factory=set)
    _noticed: dict[int, list[Noticed]] = field(default_factory=dict)   # npc_id -> not yet told to the model
    _sent: dict[int, str] = field(default_factory=dict)                # npc_id -> profile the game last got

    def ingest(self, raws: list[str]) -> int:
        """Store each presence record once. Returns how many changed someone's feelings."""
        identities = IdentityRegistry(self.store)
        changed = 0
        for raw in raws:
            try:
                p = P.parse(raw)
            except (BadRecord, ValueError) as e:
                logger.error(f"Living World body language: record skipped ({e}): {raw[:200]}")
                continue
            if p.key in self._seen_keys:
                continue
            self._seen_keys.add(p.key)
            npc_id = identities.ensure_npc(p.target.identity(), p.game_time)
            player_id = identities.ensure_npc(p.actor.identity(), p.game_time)
            with self.store.transaction() as db:
                if is_child(db, npc_id):
                    continue
                feelings = state.load(db, npc_id, p.game_time)
                deltas = P.appraise(p, traits_of(db, npc_id), P.bond_of(db, npc_id, player_id))
                if deltas:
                    feelings.add(deltas)
                    state.save(db, npc_id, feelings, p.game_time)
                    changed += 1
            text = P.observation(p, p.actor.name)
            if text:
                self._noticed.setdefault(npc_id, []).append(Noticed(p.game_time, text))
            logger.log(23, f"Living World body language: {p.target.name} noticed {p.what} "
                           f"(dist {p.distance_m}, dur {p.duration_s}, n {p.count}); feelings now "
                           f"{ {k: v for k, v in feelings.to_json().items() if v} }")
        return changed

    def prompt_lines(self, npc_id: int, name: str, now: float) -> list[str]:
        """What ``name`` noticed the player doing, each thing told once, plus how it left them feeling."""
        fresh = [n for n in self._noticed.pop(npc_id, [])
                 if -5.0 <= (now - n.game_time) * state.MIN_PER_DAY <= P.OBSERVATION_MINUTES + 1e-6]
        # (a record may be stamped a little after the context's game time, which the game sends less often)
        lines = [n.text for n in fresh[-P.MAX_OBSERVATIONS:]]
        if lines:
            with self.store.transaction() as db:
                felt = P.felt_line(name, state.load(db, npc_id, now))
            if felt:
                lines.append(felt)
        return lines

    def profiles(self, people: dict[int, str], player_id: int | None, now: float) -> list[str]:
        """Expression profiles that changed since the game last got them (``Name|comfort|gaze|guard|step``)."""
        out = []
        with self.store.transaction() as db:
            for npc_id, name in people.items():
                if npc_id == player_id or is_child(db, npc_id):
                    continue
                bond = P.bond_of(db, npc_id, player_id) if player_id is not None else P.Bond()
                wire = P.profile(name, traits_of(db, npc_id), bond, state.load(db, npc_id, now)).wire()
                if self._sent.get(npc_id) != wire:
                    self._sent[npc_id] = wire
                    out.append(wire)
        return out

    def forget_sent(self) -> None:
        """A new conversation: the game may have lost what it was sent (a load), so send everything again."""
        self._sent = {}

    def clamp_mood(self, npc_id: int, score: int, previous: int | None, now: float) -> int:
        with self.store.transaction() as db:
            return P.clamp_mood_score(score, previous, state.load(db, npc_id, now))
