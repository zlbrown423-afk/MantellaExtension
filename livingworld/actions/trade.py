"""Trade: swap the goods and gold of a deal the player and an NPC agreed in conversation.

Mantella's Barter only works on merchants (a guard has no barter menu, so Skyrim silently ignores it) and
Inventory only opens a window. When the player talks a guard into "100 gold for two amethysts", nothing
carried the deal out. The Trade action (trade.json) names what each side hands over; this module tidies
the LLM's arguments into the plain fields the game reads (Mantella-Spell ``LwTrade`` in
MantellaRepository.psc), which checks both sides have the goods and then moves them in one go.

On with the Living World unless ``MANTELLA_LW_TRADE=0``.
"""
from __future__ import annotations

import os
import re

TRADE_ENV = "MANTELLA_LW_TRADE"
_OFF = {"0", "false", "no", "off"}

MAX_COUNT = 1000
MAX_GOLD = 100000

_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "a pair of": 2, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a dozen": 12, "dozen": 12}


def trade_enabled() -> bool:
    return os.environ.get(TRADE_ENV, "").strip().lower() not in _OFF


def _to_int(value, default: int, ceiling: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = str(value or "").strip().lower()
        digits = re.search(r"\d[\d,]*", text)
        if digits:
            n = int(digits.group(0).replace(",", ""))
        elif text in _WORD_NUMBERS:
            n = _WORD_NUMBERS[text]
        else:
            return default
    return max(0, min(n, ceiling))


def _split_item(item, count, default_count: int = 1) -> tuple[str, int, int]:
    """'2 amethysts' / 'two flawless amethysts' / '50 gold' -> (name, count, gold carried in the item)."""
    name = re.sub(r"\s+", " ", str(item or "")).strip(" .,'\"")
    lowered = name.lower()
    lead = None
    for words in sorted(_WORD_NUMBERS, key=len, reverse=True):
        if lowered.startswith(words + " "):
            lead = _WORD_NUMBERS[words]
            name = name[len(words) + 1:]
            break
    if lead is None:
        m = re.match(r"(\d[\d,]*)\s*x?\s+(.*)", name)
        if m:
            lead = int(m.group(1).replace(",", ""))
            name = m.group(2)
    name = re.sub(r"^(?:of\s+)?(?:the|my|your|his|her|those|these)\s+", "", name.strip(), flags=re.IGNORECASE)
    n = _to_int(count, 0, MAX_COUNT) or lead or default_count
    n = max(1, min(n, MAX_COUNT))
    if re.fullmatch(r"(?:gold|septims?|coins?|gold coins?|gold pieces?)", name.lower()):
        return "", 0, n
    return name, (n if name else 0), 0


def _singular(word: str) -> str:
    w = word
    if len(w) > 4 and w.lower().endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.lower().endswith(("ches", "shes", "xes")):
        return w[:-2]
    if len(w) > 3 and w.lower().endswith("s") and not w.lower().endswith("ss"):
        return w[:-1]
    return w


def match_terms(name: str) -> str:
    """Spellings the game can look for in item names, joined by '|': Papyrus' text search may care about
    capitals, and the LLM says 'amethysts' for an item called 'Amethyst' or 'Flawless Amethyst'."""
    if not name:
        return ""
    words = name.split()
    single = " ".join(words[:-1] + [_singular(words[-1])])
    terms: list[str] = []
    for base in (name, single):
        for variant in (base, base.lower(), base.title(), base[:1].upper() + base[1:].lower()):
            if variant and variant not in terms:
                terms.append(variant)
    return "|".join(terms)


def normalize(arguments: dict) -> dict:
    """The deal in the fields the game reads; ``trade_ok`` is False when nothing would change hands."""
    args = dict(arguments or {})
    for side in ("player", "npc"):
        name, count, gold_in_item = _split_item(args.get(f"{side}_gives_item"), args.get(f"{side}_gives_count"))
        gold = _to_int(args.get(f"{side}_gives_gold"), 0, MAX_GOLD) + gold_in_item
        args[f"{side}_gives_item"] = name
        args[f"{side}_gives_count"] = count
        args[f"{side}_gives_gold"] = min(gold, MAX_GOLD)
        args[f"{side}_gives_match"] = match_terms(name)
    # Gold both ways nets out: only the difference moves.
    if args["player_gives_gold"] and args["npc_gives_gold"]:
        net = args["player_gives_gold"] - args["npc_gives_gold"]
        args["player_gives_gold"], args["npc_gives_gold"] = max(net, 0), max(-net, 0)
    args["trade_ok"] = bool(args["player_gives_item"] or args["player_gives_gold"]
                            or args["npc_gives_item"] or args["npc_gives_gold"])
    return args


def describe(args: dict) -> str:
    def side(item: str, count: int, gold: int) -> str:
        parts = []
        if item:
            parts.append(f"{count} {item}")
        if gold:
            parts.append(f"{gold} gold")
        return " and ".join(parts) or "nothing"
    return (f"player gives {side(args['player_gives_item'], args['player_gives_count'], args['player_gives_gold'])}, "
            f"NPC gives {side(args['npc_gives_item'], args['npc_gives_count'], args['npc_gives_gold'])}")
