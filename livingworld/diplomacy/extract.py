"""Reading diplomacy out of a conversation, on the extraction call that already runs (no new AI call).

``memory/extraction.py`` asks one AI call per NPC for that NPC's memories. When diplomacy is on and the
conversation is political (``is_political``), exactly one of those calls, the scribe's (``scribe``), also asks
for a ``diplomacy`` list. The list is then checked line by line in ``record.py``; the model's labels never
outrank what the line actually says.
"""
from __future__ import annotations

import re

from livingworld.diplomacy import ISSUES, KINDS, MAX_ITEMS, MODALITIES, RESPONSES
from livingworld.diplomacy import figures as F

# words that make a conversation political enough to ask about (cheap, on the transcript only)
_POLITICAL = re.compile(
    r"\b(ulfric|tullius|stormcloaks?|imperials?|empire|legion|legionnaires?|thalmor|dominion|elenwen|concordat|"
    r"white[- ]gold|civil war|the war|rebellion|rebels?|cease[- ]?fire|truce|armistice|alliance|ally|allies|"
    r"peace|treaty|negotiat\w*|parley|council|summit|envoy|emissary|ambassador|high king|moot|talos)\b", re.I)

SECTION = """
Also return "diplomacy": politics said in this conversation about the civil war, the Empire, the Stormcloaks,
the Thalmor, the White-Gold Concordat, a truce, an alliance, or a meeting between leaders. [] if there was none.
At most {max_items} items, each:
{{"line": <line number>, "said_by": "<name>", "whose": "<whose view, proposal or promise it is>", "to": "<name or ''>",
 "kind": "<kind>", "issue": "<issue>", "stance": <-1.0 to 1.0 or null>, "modality": "<modality>",
 "summary": "<short neutral phrase that continues the name in whose, e.g. 'will not discuss a truce while Talos is banned'>",
 "condition": "<condition or ''>", "venue": "<place or ''>", "response": "<{responses} or ''>", "due_days": <number or null>}}
kind: {kinds}.
issue: {issues}.
modality: asserted (meant as said), conditional (only if something), hypothetical (what if, suppose), question,
 reported (passing on what someone else said or did), rumor (hearsay).
Diplomacy rules:
- Record only what was said on that line. Never guess what anyone believes or will do. Courtesy is not agreement.
- whose: the person whose view, proposal or promise it is. If said_by passes on someone else's view, kind is report.
- commitment: whose promises to do something (to = who it was promised to). fulfilment: whose says they did what
  they promised. retraction: whose takes back an earlier promise or offer.
- invitation: whose asks "to" to attend a meeting (venue = where). invitation_response: whose answers an invitation
  made to them; response says how; condition holds any conditions; venue holds a different place they propose.
- stance: for position, support and opposition, how far whose is for (1) or against (-1) the issue.
- due_days: only when a time limit was said ("within three days" = 3).
"""


def is_political(transcript: str, participant_ids: list[int], db) -> bool:
    if any(F.figure_of(db, npc_id) is not None for npc_id in participant_ids):
        return True
    return bool(_POLITICAL.search(transcript or ""))


def section() -> str:
    return SECTION.format(max_items=MAX_ITEMS, kinds=", ".join(KINDS), issues=", ".join(ISSUES),
                          responses="|".join(RESPONSES))


def json_shape_note() -> str:
    return ' Add the key "diplomacy" to the JSON object.'


def scribe(store, conv_id: int, participants: list[tuple[int, str, bool]], lines: list) -> int | None:
    """The one NPC whose extraction also reads the politics: the adult NPC who heard the most lines (lowest
    npc_id on a tie), so a conversation is read once however many NPCs took part."""
    from livingworld.store.minors import is_child
    best, best_heard = None, -1
    with store.reading() as db:
        for npc_id, _name, is_player in participants:
            if is_player or is_child(db, npc_id):
                continue
            row = db.execute("SELECT joined_seq, left_seq FROM participant WHERE conv_id = ? AND npc_id = ?",
                             (conv_id, npc_id)).fetchone()
            if row is None:
                continue
            joined, left = row
            heard = sum(1 for l in lines if l.seq > joined and (left is None or l.seq <= left))
            if heard > best_heard or (heard == best_heard and best is not None and npc_id < best):
                best, best_heard = npc_id, heard
    return best


def items_from(data: dict) -> list[dict]:
    raw = data.get("diplomacy") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, dict)][:MAX_ITEMS * 2]   # validation trims to MAX_ITEMS


__all__ = ["is_political", "section", "scribe", "items_from", "MODALITIES"]
