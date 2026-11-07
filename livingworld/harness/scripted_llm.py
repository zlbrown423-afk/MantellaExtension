"""A stand-in for the LLM that returns scripted replies and keeps every prompt it was sent.

It makes no network calls, so tests and replays run offline and spend no API quota.
Keeping the prompts lets later milestones assert what each NPC was (and was not) told,
for example that Hod's prompt never contains Gerdur's private memories.
"""
from __future__ import annotations

import json
import re

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from src.llm.ai_client import AIClient
from src.llm.client_base import VisionMode
from src.llm.llm_model_list import LLMModelList
from src.llm.message_thread import message_thread
from src.llm.messages import Message

# A rule receives the last user message text and returns a reply, or None to pass.
ReplyRule = Callable[[str], "str | None"]


@dataclass
class LLMCall:
    """One call to the scripted LLM, in OpenAI message format."""
    kind: str  # "stream" (NPC dialogue) or "request" (summaries and other background calls)
    messages: list[dict[str, Any]]
    reply: str

    @property
    def system_prompt(self) -> str:
        for m in self.messages:
            if m.get("role") == "system":
                return _content_text(m.get("content"))
        return ""

    @property
    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "user":
                return _content_text(m.get("content"))
        return ""

    @property
    def all_text(self) -> str:
        return "\n".join(_content_text(m.get("content")) for m in self.messages)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multi-part content (eg vision)
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _to_openai(messages: Message | message_thread) -> list[dict[str, Any]]:
    if isinstance(messages, message_thread):
        return [dict(m) for m in messages.get_openai_messages()]
    return [dict(messages.get_openai_message())]


@dataclass
class ScriptedLLM(AIClient):
    """Scripted replacement for LLMClient.

    Replies are chosen in this order:
    1. the first rule in ``rules`` that returns text for the last user message
    2. the next unused entry in ``replies``
    3. ``default_reply``

    Background calls (``request_call``, used for summaries) return ``summary_reply``.
    """
    replies: list[str] = field(default_factory=list)
    rules: list[ReplyRule] = field(default_factory=list)
    default_reply: str = "Hm. Is that so?"
    summary_reply: str = "The player and the NPC had a short conversation."
    token_limit_value: int = 32000
    calls: list[LLMCall] = field(default_factory=list)
    # Living World extraction: NPC name -> scripted extraction for that NPC's memory
    extractions: dict[str, "ScriptedExtraction"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._reply_index = 0
        # attributes Mantella reads from LLMClient instances
        self._image_client = None
        self._function_client = None
        self._vision_mode = VisionMode.DISABLED

    # --- AIClient interface ---------------------------------------------------
    async def streaming_call(self, messages: Message | message_thread, is_multi_npc: bool, tools: list[dict] | None = None) -> AsyncGenerator[tuple[str, str | list] | None, None]:
        openai_messages = _to_openai(messages)
        reply = self._choose_reply(openai_messages)
        self.calls.append(LLMCall("stream", openai_messages, reply))
        # yield word by word, like a streamed response
        words = reply.split(" ")
        for i, word in enumerate(words):
            yield ("content", word + (" " if i < len(words) - 1 else ""))

    def request_call(self, messages: Message | message_thread) -> str | None:
        openai_messages = _to_openai(messages)
        reply = self.summary_reply
        system = _content_text(openai_messages[0].get("content")) if openai_messages else ""
        match = EXTRACTION_HEADER.match(system)
        if match:
            scripted = self.extractions.get(match.group(1))
            user = _content_text(openai_messages[-1].get("content"))
            reply = scripted.reply(user) if scripted else '{"facts": [], "memories": []}'
        self.calls.append(LLMCall("request", openai_messages, reply))
        return reply

    @property
    def extraction_calls(self) -> list[LLMCall]:
        return [c for c in self.calls if c.kind == "request" and EXTRACTION_HEADER.match(c.system_prompt)]

    def get_count_tokens(self, messages: message_thread | list[Message] | Message | str) -> int:
        # rough estimate (about 4 characters per token); good enough for prompt-size checks
        if isinstance(messages, str):
            text = messages
        elif isinstance(messages, Message):
            text = messages.get_formatted_content()
        else:
            text = " ".join(m.get_formatted_content() for m in messages)
        return max(1, len(text) // 4)

    def is_too_long(self, messages: message_thread | list[Message] | Message | str, token_limit_percent: float) -> bool:
        return self.get_count_tokens(messages) > self.token_limit_value * token_limit_percent

    @staticmethod
    def get_model_list(service: str, default_model: str = "scripted", is_vision: bool = False, is_tool_calling: bool = False, show_key_error: bool = False) -> LLMModelList:
        return LLMModelList([("Scripted", "scripted")], "scripted", allows_manual_model_input=False)

    # --- attributes and methods Mantella uses on LLMClient --------------------
    @property
    def token_limit(self) -> int:
        return self.token_limit_value

    @property
    def model_name(self) -> str:
        return "scripted"

    @property
    def is_local(self) -> bool:
        return True

    def enable_vision_for_next_call(self) -> None:
        pass

    def _determine_vision_mode(self) -> VisionMode:
        return VisionMode.DISABLED

    # --- helpers --------------------------------------------------------------
    @property
    def dialogue_calls(self) -> list[LLMCall]:
        return [c for c in self.calls if c.kind == "stream"]

    @property
    def background_calls(self) -> list[LLMCall]:
        return [c for c in self.calls if c.kind == "request"]

    def _choose_reply(self, openai_messages: list[dict[str, Any]]) -> str:
        last_user = ""
        for m in reversed(openai_messages):
            if m.get("role") == "user":
                last_user = _content_text(m.get("content"))
                break
        for rule in self.rules:
            reply = rule(last_user)
            if reply:
                return reply
        if self._reply_index < len(self.replies):
            reply = self.replies[self._reply_index]
            self._reply_index += 1
            return reply
        return self.default_reply


def when_player_says(phrase: str, reply: str) -> ReplyRule:
    """Rule: reply with ``reply`` when the last user message contains ``phrase`` (case-insensitive)."""
    phrase_lower = phrase.lower()
    return lambda text: reply if phrase_lower in text.lower() else None


EXTRACTION_HEADER = re.compile(r"You maintain the memory of one character in Skyrim, (.+?)\.\n")


@dataclass
class Fact:
    """A fact the scripted extractor reports, found by a phrase in the transcript it is shown."""
    phrase: str
    about: str
    predicate: str
    value: str
    said_by: str


@dataclass
class ScriptedExtraction:
    facts: list[Fact] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    raw_facts: list[dict[str, Any]] = field(default_factory=list)  # passed through unchecked
    reports: list[dict[str, Any]] = field(default_factory=list)    # passed through unchecked

    def reply(self, transcript: str) -> str:
        facts = list(self.raw_facts)
        for fact in self.facts:
            for line in transcript.splitlines():
                m = re.match(r"\[(\d+)\] ([^:]+): (.*)", line)
                if m and fact.phrase.lower() in m.group(3).lower():
                    facts.append({"about": fact.about, "predicate": fact.predicate, "value": fact.value,
                                  "said_by": fact.said_by, "line": int(m.group(1))})
                    break
        reports = []
        for report in self.reports:
            report = dict(report)
            phrase = report.pop("phrase", None)
            if phrase:   # find the line the report was said on, like Fact does
                for line in transcript.splitlines():
                    m = re.match(r"\[(\d+)\] ([^:]+): (.*)", line)
                    if m and phrase.lower() in m.group(3).lower():
                        report.setdefault("line", int(m.group(1)))
                        report.setdefault("said_by", m.group(2))
                        break
            reports.append(report)
        return json.dumps({"facts": facts, "memories": [{"text": t, "importance": 0.6} for t in self.memories],
                           "reports": reports})
