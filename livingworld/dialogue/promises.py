"""Keep your word: an NPC who says they are about to sit down or have a drink actually does it.

The LLM often says "I'll sit and have a drink with you" without starting its reply with ``Sit:`` /
``Drink:`` (or calling the tool), so nothing happens in the game. This parser sits in the reply parser
chain after Mantella's own actions parser and reads each spoken sentence of the NPC. When the sentence
is a first-person promise for right now ("I'll sit", "let me take a seat", "I could use a drink, let me
have one") and the reply has not already started that action, it adds the action to the sentence, so
the game carries it out when the line is spoken.

Deliberately narrow, so it never makes an NPC do something they did not say:
- only the NPC's own speech, never narration or system lines;
- only statements: a question ("Shall I sit?"), a refusal ("I won't sit with the likes of you") or a
  promise for later ("I'll have a drink once my shift ends") does nothing;
- only the actions the conversation actually offers (one-on-one / group rules still apply);
- each action at most once per reply.

"Let me get us drinks" is not handled here: fetching a round costs gold, so that stays OrderDrinks,
which the LLM must choose itself.

On with the Living World unless ``MANTELLA_LW_KEEP_PROMISES=0``.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

from livingworld.actions import DRINK, SIT

PROMISES_ENV = "MANTELLA_LW_KEEP_PROMISES"
_OFF = {"0", "false", "no", "off"}

logger = logging.getLogger("livingworld.promises")


def promises_enabled() -> bool:
    return os.environ.get(PROMISES_ENV, "").strip().lower() not in _OFF


# "I'll", "let me", "I'm going to" ... : the speaker commits to doing it now.
_LEAD = (r"(?:\bi\s*'?\s*ll\b|\bi\s+will\b|\blet\s+me\b|\blet\s*'?\s*s\b|\blet\s+us\b|\bi\s*'?\s*m\s+(?:going\s+to|gonna)\b"
         r"|\bi\s+shall\b|\bi\s+could\s+(?:do\s+with|use)\b|\btime\s+for\s+(?:me\s+to|a)\b)")
_GAP = r"[^.!?;]{0,40}?"   # a few words between: "I'll just go and sit down", "let me pull up a stool"

_SIT = re.compile(_LEAD + _GAP + r"\b(?:sit|sitting|take\s+a\s+seat|have\s+a\s+seat|grab\s+a\s+seat|find\s+a\s+seat"
                  r"|pull\s+up\s+a\s+(?:chair|stool|bench)|rest\s+my\s+(?:legs|feet|bones)|take\s+the\s+weight\s+off)\b",
                  re.IGNORECASE)
_DRINK = re.compile(_LEAD + _GAP + r"\b(?:(?:have|grab|get|pour|enjoy|take|try|finish)\s+(?:myself\s+)?"
                    r"(?:a|an|one|some|another|my|this|that)?\s*(?:drink|mead|ale|wine|sip|swig|swallow|tankard|mug|cup"
                    r"|flagon|pint|bottle|dram)s?\b|drink\b|sip\b)", re.IGNORECASE)

# The promise is off, conditional or for later.
_NOT_NOW = re.compile(r"\b(?:not|never|n't|no\s+time|rather\s+not|if|once|later|tomorrow|tonight|after|when"
                      r"|someday|some\s+other|another\s+time|next\s+time|before\s+long)\b|n\s*'?\s*t\b", re.IGNORECASE)

# Politeness, not a condition: "I'll sit with you, if you don't mind".
_POLITE = re.compile(r"\bif\s+(?:you\s+(?:don't|do\s+not)\s+mind|you\s+(?:'d\s+|would\s+)?like|you\s+please|that'?s\s+(?:all\s*right|alright|ok(?:ay)?)"
                     r"|it'?s\s+(?:all\s*right|alright|ok(?:ay)?))\b", re.IGNORECASE)

# For another person, not the speaker: "I'll get you a drink", "let me pour you some mead".
_FOR_OTHERS = re.compile(r"\b(?:you|us|him|her|them|everyone|yourself)\b", re.IGNORECASE)


def promised_actions(text: str) -> list[str]:
    """The actions this sentence promises to do right now, in the order they are named."""
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    if not text.strip() or text.rstrip().endswith("?"):
        return []
    found: list[tuple[int, str]] = []
    for identifier, pattern in ((SIT, _SIT), (DRINK, _DRINK)):
        for match in pattern.finditer(text):
            span = match.group(0)
            rest_of_clause = re.split(r"[.!?;]", text[match.start():], maxsplit=1)[0]
            if _NOT_NOW.search(_POLITE.sub("", rest_of_clause)):
                continue
            if identifier == DRINK and _FOR_OTHERS.search(_object_part(span)):
                continue
            found.append((match.start(), identifier))
            break
    return [identifier for _, identifier in sorted(found)]


def _object_part(span: str) -> str:
    """The words after the lead, where 'you' / 'us' would name who the drink is for."""
    lead = re.match(_LEAD, span, re.IGNORECASE)
    rest = span[lead.end():] if lead else span
    return re.sub(r"\bjoin\s+you\b|\bwith\s+you\b", "", rest, flags=re.IGNORECASE)


class PromiseKeeper:
    """One per reply: adds the sit / drink action to the sentence that promises it."""

    def __init__(self, actions: Iterable[Any]):
        self._offered = {getattr(a, "identifier", None) for a in actions} & {SIT, DRINK}
        self._done: set[str] = set()

    def note_actions(self, actions: Iterable[Any]) -> None:
        """Actions the reply already started (``Sit:`` prefix or a tool call): never add them again."""
        for action in actions:
            identifier = action.get("identifier") if isinstance(action, dict) else None
            if identifier:
                self._done.add(identifier)

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        """Append the promised, not yet started actions to ``sentence_actions``; returns what was added."""
        self.note_actions(sentence_actions)
        added: list[str] = []
        if not self._offered:
            return added
        for identifier in promised_actions(text):
            if identifier in self._offered and identifier not in self._done:
                sentence_actions.append(_action(identifier, speaker_name))
                self._done.add(identifier)
                added.append(identifier)
        if added:
            logger.log(23, f"Living World: {speaker_name} said they would {', '.join(_verb(i) for i in added)}; doing it")
        return added


def _action(identifier: str, speaker_name: str) -> dict:
    # The advanced form, so the game knows exactly who and what: any free seat nearby, not "pointed".
    if identifier == SIT:
        return {"identifier": SIT, "arguments": {"source": [speaker_name], "furniture": "seat"}}
    return {"identifier": identifier, "arguments": {"source": speaker_name}}


def _verb(identifier: str) -> str:
    return {SIT: "sit down", DRINK: "have a drink"}.get(identifier, identifier)


def make_parser(actions: Iterable[Any]):
    """A Mantella output parser that keeps the NPC's promises (see module docstring)."""
    from src.llm.output.output_parser import output_parser
    from src.llm.sentence_content import SentenceTypeEnum

    class PromiseParser(output_parser):
        def __init__(self) -> None:
            super().__init__()
            self.keeper = PromiseKeeper(actions)

        def cut_sentence(self, output, current_settings):
            return None, output

        def modify_sentence_content(self, cut_content, last_content, settings):
            if cut_content.sentence_type == SentenceTypeEnum.SPEECH and not cut_content.is_system_generated_sentence:
                self.keeper.keep(cut_content.speaker.name, cut_content.text, cut_content.actions)
            else:
                self.keeper.note_actions(cut_content.actions)
            return cut_content, last_content

        def get_cut_indicators(self):
            return []

    return PromiseParser()
