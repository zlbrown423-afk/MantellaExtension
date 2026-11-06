"""Mantella's ``Remembering`` interface with each NPC's own recalled rows added to the prompt."""
from __future__ import annotations

from typing import Any

from src.remember.remembering import Remembering


class LivingWorldMemory(Remembering):
    """Delegates to Mantella's summaries and adds the Living World recall after them.

    Saving still goes to Mantella's summaries; the Living World side is fed by the conversation
    log and the extraction job instead.
    """

    def __init__(self, inner: Remembering, living_world: Any) -> None:
        self.inner = inner
        self.living_world = living_world

    def get_prompt_text(self, characters, world_id: str) -> str:
        base = self.inner.get_prompt_text(characters, world_id)
        recalled = self.living_world.recall_text(characters, world_id) or ""
        return "\n\n".join(part for part in (base, recalled) if part)

    def save_conversation_state(self, *args: Any, **kwargs: Any):
        return self.inner.save_conversation_state(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
