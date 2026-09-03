import json

from globals import logger
from src.configs.constant import RANGER_MEMORY_SOKT_FLOW_URL
from src.services.cache_service import store_in_cache
from src.services.utils.gpt_memory import _fetch_memory_from_cache, _is_empty_memory_response, parse_memory

from ..utils.apiservice import fetch

# Reused from gpt_memory.py rather than duplicated; only the remote-fetch shape below differs.


async def _fetch_memory_from_remote(cache_id: str, key: str, collection: str):
    if not RANGER_MEMORY_SOKT_FLOW_URL:  # safe no-op (cache-only) if ever unset
        return None
    try:
        response, _ = await fetch(
            RANGER_MEMORY_SOKT_FLOW_URL, "POST", None, None, {"key": key, "collection": collection}
        )
        if response is None or _is_empty_memory_response(response):
            return None
        await store_in_cache(cache_id, response)
        return response
    except Exception as err:
        logger.error(f"Error fetching ranger/user memory from remote for {cache_id}: {str(err)}")
        return None


async def _get_scoped_memory(cache_id: str, key: str, collection: str):
    memory = await _fetch_memory_from_cache(cache_id)
    if memory is None:
        memory = await _fetch_memory_from_remote(cache_id, key, collection)
    return memory


async def get_ranger_memory(bridge_id: str | None):
    """Ranger memory: persists per Ranger (bridge), shared across every user/thread of that Ranger."""
    if not bridge_id:
        return None
    try:
        raw_memory = await _get_scoped_memory(f"ranger_{bridge_id}", bridge_id, "ranger_memory")
        return parse_memory(raw_memory)
    except Exception as err:
        logger.error(f"Error getting ranger memory for bridge {bridge_id}: {str(err)}")
        return None


async def get_user_memory(user_id: str | None):
    """User memory: persists per Ranger-owner, shared across every Ranger that user created."""
    if not user_id:
        return None
    try:
        raw_memory = await _get_scoped_memory(f"user_{user_id}", user_id, "user_memory")
        return parse_memory(raw_memory)
    except Exception as err:
        logger.error(f"Error getting user memory for user {user_id}: {str(err)}")
        return None


def compose_memory_context(thread_memory=None, ranger_memory=None, user_memory=None) -> str:
    """Label and join whichever of the thread/ranger/user memory sources are present into one string."""
    try:
        parts = []

        def _stringify(value):
            if value is None or value == "":
                return None
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)

        thread_str = _stringify(thread_memory)
        if thread_str:
            parts.append(f"Thread memory:\n{thread_str}")

        ranger_str = _stringify(ranger_memory)
        if ranger_str:
            parts.append(f"Ranger memory:\n{ranger_str}")

        user_str = _stringify(user_memory)
        if user_str:
            parts.append(f"User memory:\n{user_str}")

        return "\n\n".join(parts)
    except Exception as err:
        logger.error(f"Error composing memory context: {str(err)}")
        return thread_memory if isinstance(thread_memory, str) else ""
