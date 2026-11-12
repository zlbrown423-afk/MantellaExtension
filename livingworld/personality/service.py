"""The personality subsystem as the rest of Living World sees it: one object per server, called from
``integration.py`` at conversation start, for prompt lines, on each line and at the end. Every call takes an open
database (inside the caller's transaction) and never raises into Mantella (the caller catches and logs).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from livingworld.personality import admin, bridge, debug, dialogue, discovery, evolve, prompt
from livingworld.personality import profile as P
from livingworld.personality.config import SettingsFile, default_path

logger = logging.getLogger(__name__)


class PersonalityService:
    def __init__(self, save_folder: str, settings_path: str | None = None) -> None:
        self.save_folder = save_folder
        self.file = SettingsFile(settings_path or default_path(save_folder))
        bridge.use_settings(self.file.get)
        self._shareable: dict[int, list[str]] = {}    # npc_id -> items hers to share with the player this turn

    def settings(self):
        return self.file.get()

    def profiles(self, db, npc_ids, now: float) -> dict[int, P.Profile]:
        out = {}
        for npc_id in npc_ids:
            p = P.resolve(db, npc_id, self.settings(), now)
            if p.active:
                out[npc_id] = p
        return out

    # ---- conversation ----
    def started(self, db, npc_ids: list[int], player_id: int | None, now: float, conv_id: int | None) -> None:
        self._shareable = {}
        for npc_id, p in self.profiles(db, npc_ids, now).items():
            res = evolve.evaluate(db, npc_id, self.settings(), now, cause=f"conv:{conv_id}" if conv_id else None)
            for change in res.changes:
                logger.log(23, f"Living World personality: {p.name}: {change[0]} {change[1]} {change[2]} ({change[3]})")
            if player_id is not None:
                learned = discovery.observe(db, p, player_id, now)
                if learned:
                    logger.log(23, f"Living World personality: the player now suspects {p.name}'s {', '.join(learned)}")

    def ended(self, db, npc_ids: list[int], now: float, conv_id: int | None) -> None:
        for npc_id in self.profiles(db, npc_ids, now):
            evolve.evaluate(db, npc_id, self.settings(), now, cause=f"conv:{conv_id}" if conv_id else None)

    def standing(self, db, npc_id: int, player_id: int | None, player_name: str, now: float) -> list[str]:
        p = self.profiles(db, [npc_id], now).get(npc_id)
        if p is None:
            return []
        lines = prompt.standing_lines(db, p, player_id, player_name, _sex(db, player_id))
        if player_id is not None:
            style = dialogue.style_lines(self.dialogue_of(db, p), p.name, player_name,
                                         moment(db, p.npc_id, player_id, False, now), topic=False)
            if style:
                lines = style + lines if lines else style
        return lines

    def dialogue_of(self, db, p: P.Profile) -> "dialogue.Dialogue":
        from livingworld.personality.profile import entry_for
        from livingworld.social.traits import traits_of
        entry = entry_for(db, self.settings(), p.npc_id)
        return dialogue.resolve(p, traits_of(db, p.npc_id), entry.dialogue if entry else None,
                                self.settings().dialogue_enabled)

    def notes(self, db, text: str, present: dict[int, str], player_id: int, player_name: str, now: float) -> list[str]:
        """Notes for the AI on the player's line, one block per woman present, only when the line is on topic."""
        listening = len(present) > 1
        blocks = []
        self._shareable = {}
        for npc_id, p in self.profiles(db, list(present), now).items():
            lines = prompt.topic_lines(db, p, player_id, player_name, text, listening, _sex(db, player_id))
            if not lines:
                continue
            style = dialogue.style_lines(self.dialogue_of(db, p), p.name, player_name,
                                         moment(db, npc_id, player_id, listening, now), topic=True, salt=text)
            lines = style + lines
            g = discovery.gate(db, p, player_id, listening)
            self._shareable[npc_id] = discovery.shareable(p, g)
            blocks.append(f"{p.name}'s heart and desires (hers alone; {player_name} cannot see this):\n"
                          + "\n".join(f"- {line}" for line in lines))
        return blocks

    def npc_said(self, db, npc_id: int, player_id: int, text: str, now: float, conv_id: int | None) -> list[str]:
        allowed = self._shareable.get(npc_id)
        if not allowed:
            return []
        p = self.profiles(db, [npc_id], now).get(npc_id)
        if p is None:
            return []
        learned = discovery.heard(db, p, player_id, text, allowed, now, conv_id)
        if learned:
            logger.log(23, f"Living World personality: {p.name} told the player about {', '.join(learned)}")
        return learned

    # ---- debug ----
    def debug(self, db, text: str, present: dict[int, str], player_id: int | None, now: float) -> str | None:
        if not debug.enabled():
            return None
        command = debug.parse(text)
        if command is None:
            return None
        done = []
        for npc_id, name in present.items():
            if npc_id == player_id:
                continue
            if command == "reset":
                admin.reset(db, self.file, npc_id, now)
            elif command == "regenerate":
                admin.regenerate(db, npc_id, now)
            elif command == "evolve":
                evolve.evaluate(db, npc_id, self.settings(), now, cause="debug")
            data = (admin.player_view(db, self.settings(), npc_id, player_id, now) if command == "known"
                    and player_id is not None else admin.inspect(db, self.settings(), npc_id, now, player_id))
            self._write(name, data)
            logger.log(23, f"Living World personality ({command}) {name}: {json.dumps(data, ensure_ascii=False)}")
            done.append(name)
        return f"{command}: {', '.join(done)}" if done else None

    def _write(self, name: str, data: dict[str, Any]) -> None:
        folder = os.path.join(os.path.dirname(os.path.abspath(self.file.path)), "personality-inspect")
        try:
            os.makedirs(folder, exist_ok=True)
            safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "npc"
            with open(os.path.join(folder, f"{safe}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError:
            logger.exception("Living World personality: could not write the inspect file")


def _sex(db, npc_id: int | None) -> str | None:
    if npc_id is None:
        return None
    row = db.execute("SELECT sex FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else None


def moment(db, npc_id: int, player_id: int, others_listening: bool, now: float) -> "dialogue.Moment":
    """What weighs on how far she goes right now: her trust and attraction toward the player, company, mood."""
    from livingworld.social.consent import _mood
    row = db.execute("SELECT r.trust, r.attraction, b.status FROM v_relationship r JOIN relationship b"
                     " ON b.from_id = r.from_id AND b.to_id = r.to_id WHERE r.from_id = ? AND r.to_id = ?",
                     (npc_id, player_id)).fetchone()
    trust, attraction, status = row if row else (0.0, 0.0, None)
    score, _label = _mood(db, npc_id, now)
    return dialogue.Moment(trust, attraction, others_listening, score, status in ("courting", "partner", "spouse"))
