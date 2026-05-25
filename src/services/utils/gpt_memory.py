import json
from typing import Any

from globals import logger
from src.services.cache_service import find_in_cache, store_in_cache

from ..utils.apiservice import fetch


def _deserialize_cached_value(raw_value):
    """Decode Redis payloads into native python structures."""
    if raw_value is None:
        return None
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8")
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def _is_empty_memory_response(value) -> bool:
    """Sokt flow returns {'success': True, 'message': 'No response'} when no memory exists for the thread."""
    if not isinstance(value, dict):
        return False
    return value.get("success") is True and value.get("message") == "No response"


def parse_memory(raw):
    """Normalize a memory payload to either a dict (new structured form), a string (legacy), or None."""
    parsed = _deserialize_cached_value(raw)
    if parsed is None or _is_empty_memory_response(parsed):
        return None
    if isinstance(parsed, dict):
        return parsed
    return str(parsed)


async def _fetch_memory_from_cache(memory_id: str):
    cached_value = await find_in_cache(memory_id)
    return _deserialize_cached_value(cached_value)


async def _fetch_memory_from_remote(memory_id: str):
    try:
        response, _ = await fetch("https://flow.sokt.io/func/scriCJLHynCG", "POST", None, None, {"threadID": memory_id})
        if response is None or _is_empty_memory_response(response):
            return None
        await store_in_cache(memory_id, response)
        return response
    except Exception as err:
        logger.error(f"Error fetching GPT memory from remote for {memory_id}: {str(err)}")
        return None


def _build_memory_id(thread_id: str, sub_thread_id: str, bridge_id: str, version_id: str | None) -> str:
    version_or_bridge = (version_id or bridge_id or "").strip()
    return f"{thread_id.strip()}_{sub_thread_id.strip()}_{version_or_bridge}"


async def get_gpt_memory(
    bridge_id: str, thread_id: str, sub_thread_id: str, version_id: str | None = None
) -> tuple[str, Any | None]:
    """Return GPT memory content for the provided identifiers."""
    memory_id = _build_memory_id(thread_id, sub_thread_id, bridge_id, version_id)
    memory = await _fetch_memory_from_cache(memory_id)
    if memory is None:
        memory = await _fetch_memory_from_remote(memory_id)

    return memory_id, memory
