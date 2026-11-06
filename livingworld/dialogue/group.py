"""Group talk with one NPC per AI call.

Upstream Mantella writes every NPC's lines in one AI call from one prompt that holds every NPC's
bio and memories, and switches speaker whenever the reply says "Hod:" (audit risks R1 to R3).
Here each speaker gets their own call:

* the prompt is the one-on-one prompt built for that NPC alone, so it holds only their identity
  and their own recalled rows, plus a note saying who else is present;
* the shared conversation is shown from their side: their own past lines as the assistant,
  everyone else's as "Name: text";
* the reply can only be that NPC's. A line that tries to switch to another speaker is cut off
  there and logged, because the only characters the reply parser knows are the speaker and the player.

Who speaks: the NPC the player named first, otherwise the NPC who did not speak last. If the
speaker names another NPC in their reply, that NPC answers next, up to ``MAX_SPEAKERS`` per turn.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from src import utils
from src.characters_manager import Characters
from src.conversation.conversation_type import pc_to_npc
from src.llm.message_thread import message_thread
from src.llm.messages import AssistantMessage, Message, UserMessage
from src.llm.sentence import Sentence
from src.llm.sentence_content import SentenceContent, SentenceTypeEnum

logger = utils.get_logger()

MAX_SPEAKERS = 2

GROUP_NOTE = """

# Others present
{others} {are} also here with you and {player}. You speak only as {name}. Never write lines for {others_or} or for {player}.
Lines spoken by others are shown as "Name: what they said". Reply with {name}'s next words only, with no name in front."""


class PlainMessage(Message):
    """A message passed to the AI as written (Mantella's own classes reformat their text)."""

    def __init__(self, role: str, text: str) -> None:
        super().__init__(text)
        self.role = role

    def get_formatted_content(self) -> str:
        return self.text

    def get_openai_message(self):
        return {"role": self.role, "content": self.text}

    def get_dict_formatted_string(self) -> str:
        return str(self.get_openai_message())


def mentions(text: str, name: str) -> int:
    """Position of the first mention of ``name`` (or its first word) in ``text``, or -1."""
    best = -1
    for candidate in {name, name.split(" ")[0]}:
        if len(candidate) < 3:
            continue
        m = re.search(rf"\b{re.escape(candidate)}\b", text, re.IGNORECASE)
        if m and (best < 0 or m.start() < best):
            best = m.start()
    return best


def last_user_text(messages: message_thread) -> str:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], UserMessage):
            return messages[i].text
    return ""


def last_npc_speaker(messages: message_thread) -> str | None:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AssistantMessage):
            sentences = messages[i]._AssistantMessage__sentences
            for content in reversed(sentences):
                if content.text.strip() and not content.speaker.is_player_character:
                    return content.speaker.name
    return None


def choose_first_speaker(messages: message_thread, npcs: list[Any]) -> Any:
    text = last_user_text(messages)
    named = sorted(((mentions(text, c.name), i, c) for i, c in enumerate(npcs) if mentions(text, c.name) >= 0),
                   key=lambda t: (t[0], t[1]))
    if named:
        return named[0][2]
    last = last_npc_speaker(messages)
    for character in npcs:
        if character.name != last:
            return character
    return npcs[0]


def speaker_view(characters: Characters, speaker: Any) -> Characters:
    view = Characters()
    player = characters.get_player_character()
    if player:
        view.add_or_update_character(player)
    view.add_or_update_character(speaker)
    return view


def thread_for(speaker: Any, system_prompt: str, messages: message_thread, config: Any,
               this_turn: list[tuple[str, str]]) -> message_thread:
    """The shared conversation as ``speaker`` sees it."""
    thread = message_thread(config, system_prompt)
    pending: list[tuple[str, str]] = []

    def push(role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if pending and pending[-1][0] == role:
            pending[-1] = (role, pending[-1][1] + "\n" + text)
        else:
            pending.append((role, text))

    for i in range(1, len(messages)):
        message = messages[i]
        if isinstance(message, UserMessage):
            push("user", message.get_formatted_content())
        elif isinstance(message, AssistantMessage):
            for content in message._AssistantMessage__sentences:
                if not content.text.strip() or content.sentence_type == SentenceTypeEnum.NARRATION:
                    continue
                if content.speaker.name == speaker.name:
                    push("assistant", content.text)
                else:
                    push("user", f"{content.speaker.name}: {content.text}")
        # tool calls, tool results and images are left out of the per-speaker view
    for name, text in this_turn:
        push("user", f"{name}: {text}")
    if not pending or pending[-1][0] != "user":
        push("user", "(continue)")
    for role, text in pending:
        thread.add_message(PlainMessage(role, text))
    return thread


class TurnQueue:
    """Passes sentences to the game's queue but holds back the end-of-turn marker until every
    speaker in the turn has finished."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.spoken: list[Sentence] = []

    @property
    def is_more_to_come(self) -> bool:
        return self.inner.is_more_to_come

    @is_more_to_come.setter
    def is_more_to_come(self, value: bool) -> None:
        if value:
            self.inner.is_more_to_come = True

    def put(self, sentence: Sentence) -> None:
        if not sentence.text.strip() and not sentence.actions and sentence.is_system_generated_sentence:
            return  # one speaker's end marker, not the end of the turn
        self.spoken.append(sentence)
        self.inner.put(sentence)

    def finish(self, speaker: Any) -> None:
        self.inner.is_more_to_come = False
        self.inner.put(Sentence(SentenceContent(speaker, "", SentenceTypeEnum.SPEECH, True), "", 0))


class GroupTurn:
    def __init__(self, living_world: Any) -> None:
        self.living_world = living_world

    def run(self, chat_manager: Any, messages: message_thread, characters: Characters, queue: Any,
            actions: list, tools: list | None, game: Any) -> None:
        context = self.living_world.context
        config = context.config
        npcs = characters.get_non_player_characters()
        player = characters.get_player_character()
        first = choose_first_speaker(messages, npcs)
        order = [first]
        this_turn: list[tuple[str, str]] = []
        turn_queue = TurnQueue(queue)
        stop_event = chat_manager._ChatManager__stop_generation
        chat_manager.living_world_holds_generation = True
        try:
            while order:
                speaker = order.pop(0)
                if stop_event.is_set():
                    break
                others = [c.name for c in npcs if c.name != speaker.name]
                focused = context.focused_on(speaker)
                prompt = pc_to_npc(config).generate_prompt(focused)
                if others:
                    prompt += GROUP_NOTE.format(
                        others=", ".join(others), are="is" if len(others) == 1 else "are",
                        others_or=" or ".join(others), name=speaker.name,
                        player=player.name if player else "the player")
                thread = thread_for(speaker, prompt, messages, config, this_turn)
                before = len(turn_queue.spoken)
                started = time.perf_counter()
                status, error = "ok", None
                try:
                    asyncio.run(chat_manager.process_response(speaker, turn_queue, thread, speaker_view(characters, speaker),
                                                              actions, tools, game))
                except Exception as e:  # process_response handles its own API errors; this is a safety net
                    status, error = "error", str(e)
                    logger.exception(f"Living World: turn for {speaker.name} failed")
                latency = int((time.perf_counter() - started) * 1000)
                said = " ".join(s.text.strip() for s in turn_queue.spoken[before:]
                                if s.speaker.name == speaker.name and s.text.strip())
                self._clear_false_discard(chat_manager, npcs)
                self.living_world.record_dialogue_call(chat_manager, speaker, latency, status, error)
                if said:
                    this_turn.append((speaker.name, said))
                    spoken = {n for n, _ in this_turn}
                    addressed = sorted(((mentions(said, c.name), c) for c in npcs
                                        if c.name not in spoken and c not in order and mentions(said, c.name) >= 0),
                                       key=lambda t: t[0])
                    order.extend(c for _, c in addressed)
                if len(this_turn) >= MAX_SPEAKERS:
                    break
        finally:
            chat_manager.living_world_holds_generation = False
            turn_queue.finish(first)
            chat_manager._ChatManager__is_generating = False

    @staticmethod
    def _clear_false_discard(chat_manager: Any, npcs: list[Any]) -> None:
        """A reply that tried to speak as another NPC who IS present was cut off on purpose; that must
        not be reported back to the AI as "unrecognized character"."""
        discarded = chat_manager.discarded_character_name
        if discarded and any(mentions(discarded, c.name) >= 0 for c in npcs):
            logger.log(23, f"Living World: cut off a reply that tried to speak as {discarded}")
            chat_manager.clear_discarded_character_name()
