import json
from typing import Any

from globals import logger
from src.configs.constant import redis_keys
from src.services.cache_service import find_in_cache, store_in_cache

from ...configs.constant import bridge_ids
from ..prebuilt_prompt_service import get_specific_prebuilt_prompt_service
from ..utils.ai_call_util import call_ai_middleware
from ..utils.apiservice import fetch


async def handle_gpt_memory(id, user, assistant, purpose, gpt_memory_context, org_id):
    try:
        variables = {"threadID": id, "memory": purpose, "gpt_memory_context": gpt_memory_context}
        content = assistant.get("data", {}).get("content", "")
        configuration = {"conversation": [{"role": "user", "content": user}, {"role": "assistant", "content": content}]}
        updated_prompt = await get_specific_prebuilt_prompt_service(org_id=org_id, prompt_key="gpt_memory")
        if updated_prompt and updated_prompt.get("gpt_memory"):
            configuration["prompt"] = updated_prompt["gpt_memory"]
        message = "use the function to store the memory if the user message and history is related to the context or is important to store else don't call the function and ignore it. is purpose is not there than think its the begining of the conversation. Only return the exact memory as output no an extra text jusy memory if present or Just return False"
        response = await call_ai_middleware(
            message,
            bridge_id=bridge_ids["gpt_memory"],
            variables=variables,
            configuration=configuration,
            response_type="text",
        )
        if isinstance(response, str) and response != "False":
            cache_key = f"{redis_keys['gpt_memory_']}{id}"
            await store_in_cache(cache_key, response)
        return response
    except Exception as err:
        logger.error(f"Error calling function handle_gpt_memory =>, {str(err)}")


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


async def _fetch_memory_from_cache(memory_id: str):
    cached_value = await find_in_cache(memory_id)
    return _deserialize_cached_value(cached_value)


async def _fetch_memory_from_remote(memory_id: str):
    try:
        response, _ = await fetch("https://flow.sokt.io/func/scriCJLHynCG", "POST", None, None, {"threadID": memory_id})
        if response is not None:
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
