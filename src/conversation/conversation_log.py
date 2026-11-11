import json
import os
from pathlib import Path
import sys
import src.utils as utils
from src.character_manager import Character
from openai.types.chat import ChatCompletionMessageParam


def _lw():  # LIVINGWORLD PATCH: transcripts roll back with the save (livingworld/memory/summary_timeline.py)
    try:
        from livingworld.integration import LivingWorld
        return LivingWorld.latest
    except Exception:
        return None


def _lw_key(conversation) -> str:
    return "log:" + json.dumps(conversation, sort_keys=True, ensure_ascii=False)


def _lw_folder(path: str) -> str:
    root = str(conversation_log.game_path).replace(os.sep, '/').rstrip('/')
    folder = os.path.dirname(path).replace(os.sep, '/')
    return folder[len(root) + 1:] if folder.startswith(root + '/') else folder


def _lw_visible(path: str, world_id: str, history: list) -> list:
    lw = _lw()
    if lw is None or not history:
        return history
    keys = [_lw_key(c) for c in history]
    kept = lw.summary_lines(world_id, _lw_folder(path), keys)
    if kept is None:
        return history
    kept_set = set(kept)
    return [c for c, k in zip(history, keys) if k in kept_set]


class conversation_log:
    game_path: str = "" # <- This gets set in the __init__ of gameable. Not clean but cleaner than other options

    @staticmethod
    @utils.time_it
    def save_conversation_log(character: Character, messages: list[ChatCompletionMessageParam], world_id: str, game_days: float | None = None):
        # save conversation history

        if len(messages) > 0:
            # if this is not the first conversation
            conversation_history_file = conversation_log.__get_path_to_conversation_history_file(character, world_id)
            # transformed_messages = messages.transform_to_openai_messages(messages.get_talk_only())
            if os.path.exists(conversation_history_file):
                with open(conversation_history_file, 'r', encoding='utf-8') as f:
                    conversation_history = json.load(f)

                # add new conversation to conversation history
                conversation_history.append(messages) # append everything except the initial system prompt
            # if this is the first conversation
            else:
                directory = os.path.dirname(conversation_history_file)
                os.makedirs(directory, exist_ok=True)
                conversation_history = [messages] # wrap the first conversation in a list
            
            with open(conversation_history_file, 'w', encoding='utf-8') as f:
                json.dump(conversation_history, f, indent=4) # save everything except the initial system prompt
            lw = _lw()  # LIVINGWORLD PATCH: note when and on which timeline this conversation was kept
            if lw is not None:
                lw.summary_written(world_id, _lw_folder(conversation_history_file), _lw_key(messages), game_days)

    @staticmethod   
    @utils.time_it 
    def load_conversation_log(character: Character, world_id: str) -> list[str]:
        conversation_history_file = conversation_log.__get_path_to_conversation_history_file(character, world_id)
        if os.path.exists(conversation_history_file):
            with open(conversation_history_file, 'r', encoding='utf-8') as f:
                conversation_history = json.load(f)
            previous_conversations = []
            for conversation in _lw_visible(conversation_history_file, world_id, conversation_history):
                previous_conversations.extend(conversation)
            return previous_conversations
        else:
            return []

    @staticmethod
    @utils.time_it
    def get_conversation_log_length(character: Character, world_id: str) -> int:
        conversation_history_file = conversation_log.__get_path_to_conversation_history_file(character, world_id)
        if os.path.exists(conversation_history_file):
            with open(conversation_history_file, 'r', encoding='utf-8') as f:
                conversation_history = json.load(f)
            return sum(len(conversation) for conversation in _lw_visible(conversation_history_file, world_id, conversation_history))
        else:
            return 0

    @staticmethod    
    @utils.time_it
    def __get_path_to_conversation_history_file(character: Character, world_id: str) -> str:
        # if multiple NPCs in a conversation have the same name (eg Whiterun Guard) their names are appended with number IDs
        # these IDs need to be removed when saving the conversation
        name: str = utils.remove_trailing_number(character.name)
        non_ref_path = f"{conversation_log.game_path}/{world_id}/{name}/{name}.json"
        ref_path = f"{conversation_log.game_path}/{world_id}/{name} - {character.ref_id}/{name}.json"

        if os.path.exists(non_ref_path): # if a conversation folder already exists for this NPC, use it
            return non_ref_path
        else: # else include the NPC's reference ID in the folder name to differentiate generic NPCs
            return ref_path
