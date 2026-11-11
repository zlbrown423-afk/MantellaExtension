"""Living World actions: sit, stand up, go there, order drinks from the bar, undress and dress, and the
AgreeTask / CheckTask / ReleaseTask task actions and the EnlistRecruit / ReportDuty / PayWages guard actions, which only the server handles (drinking is
data/actions/livingworld_drink.json).

The JSON files in this folder use the same format as ``data/actions/``. ``FunctionManager.load_all_actions``
loads them too when ``MANTELLA_LIVING_WORLD=1`` (see the ``LIVINGWORLD PATCH`` there); otherwise the LLM is
never offered them. The game side is in Mantella-Spell's ``MantellaRepository.psc`` (LIVINGWORLD PATCH:
actions), which listens for each identifier below.

The NPC choosing whether to do it is left to the LLM: each description says the NPC may refuse in
character, and refusing is simply not calling the action.
"""
from __future__ import annotations

import os
from pathlib import Path

ACTIONS_DIR = Path(__file__).resolve().parent

SIT = "mantella_lw_npc_sit"
STAND_UP = "mantella_lw_npc_standup"
GO_THERE = "mantella_lw_npc_gothere"
DRINK = "mantella_npc_lw_drink"   # data/actions/livingworld_drink.json
ORDER_DRINKS = "mantella_lw_npc_orderdrinks"
UNDRESS = "mantella_lw_npc_undress"
DRESS = "mantella_lw_npc_dress"
TASK_AGREE = "mantella_lw_task_agree"       # tasks: handled on the server only (livingworld/work)
TASK_CHECK = "mantella_lw_task_check"
TASK_RELEASE = "mantella_lw_task_release"
GUARD_ENLIST = "mantella_lw_guard_enlist"   # guard careers: handled on the server (livingworld/guard); the game hands over the uniform
GUARD_REPORT = "mantella_lw_guard_report"   # guard duties: server only
GUARD_PAY = "mantella_lw_guard_pay"         # guard wages: the game hands over the gold the server names
DECENCY_FINE = "mantella_lw_decency_fine"   # a guard's fine for public nakedness: the game takes the gold the server names
ALL = (SIT, STAND_UP, GO_THERE, ORDER_DRINKS, UNDRESS, DRESS, TASK_AGREE, TASK_CHECK, TASK_RELEASE, GUARD_ENLIST,
       GUARD_REPORT, GUARD_PAY, DECENCY_FINE)


def enabled() -> bool:
    # same switch as livingworld.integration.ENV_VAR (not imported: that module pulls in the whole store)
    return os.environ.get("MANTELLA_LIVING_WORLD", "").strip().lower() in {"1", "true", "yes", "on"}


def action_files() -> list[Path]:
    """The action files to load, or none when Living World is off."""
    if not enabled():
        return []
    return sorted(ACTIONS_DIR.glob("*.json"))
