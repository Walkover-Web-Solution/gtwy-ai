import json

from globals import logger
from src.configs.constant import tag_keys

from .cache_service import (
    DEFAULT_REDIS_TTL,
    REDIS_PREFIX,
    client,
    make_json_serializable,
)


def extract_cache_tags(bridge_response: dict) -> list[str]:
    """Return deduped reverse-index tag strings for a cached bridge response."""
    bridge = (bridge_response or {}).get("bridges") or {}
    if not isinstance(bridge, dict):
        return []

    tags: set[str] = set()

    def _add(prefix: str, value) -> None:
        if value is None:
            return
        s = str(value).strip()
        if s:
            tags.add(f"{prefix}{s}")

    _add(tag_keys["agent"], bridge.get("parent_id"))

    blob_id = bridge.get("_id")
    parent_id = bridge.get("parent_id")
    if blob_id and parent_id and str(blob_id) != str(parent_id):
        _add(tag_keys["version"], blob_id)

    api_calls = bridge.get("apiCalls")
    if isinstance(api_calls, dict):
        for tool_id in api_calls.keys():
            _add(tag_keys["tool"], tool_id)

    pre_tools_data = bridge.get("pre_tools_data")
    if isinstance(pre_tools_data, list):
        for pre in pre_tools_data:
            if isinstance(pre, dict):
                _add(tag_keys["tool"], pre.get("_id"))

    folder_post_tool = bridge.get("folder_post_tool")
    if isinstance(folder_post_tool, dict):
        _add(tag_keys["tool"], folder_post_tool.get("_id"))

    apikey_object_id = bridge.get("apikey_object_id")
    if isinstance(apikey_object_id, dict):
        for cred_id in apikey_object_id.values():
            _add(tag_keys["apikey"], cred_id)

    _add(tag_keys["folder"], bridge.get("folder_id"))

    connected_agent_details = bridge.get("connected_agent_details")
    if isinstance(connected_agent_details, dict):
        for other_bridge_id in connected_agent_details.keys():
            _add(tag_keys["connected_agent"], other_bridge_id)
    agent_name_info = bridge.get("agent_name_info")
    if isinstance(agent_name_info, dict):
        for other_bridge_id in agent_name_info.keys():
            _add(tag_keys["connected_agent"], other_bridge_id)

    _add(tag_keys["wrapper"], bridge.get("wrapper_id"))

    doc_ids = bridge.get("doc_ids")
    if isinstance(doc_ids, list):
        for doc_id in doc_ids:
            _add(tag_keys["rag"], doc_id)

    return sorted(tags)


async def store_in_cache_with_tags(
    cache_key: str,
    data: dict,
    tags: list[str] | None = None,
    ttl: int = DEFAULT_REDIS_TTL,
) -> bool:
    """Atomically store the blob and register every tag in the reverse index."""
    try:
        serialized = make_json_serializable(data)
        blob_full_key = f"{REDIS_PREFIX}{cache_key}"
        ttl_int = int(ttl)
        tag_ttl = ttl_int + 600

        pipe = client.pipeline(transaction=True)
        pipe.set(blob_full_key, json.dumps(serialized), ex=ttl_int)
        for tag in tags or []:
            tag_full_key = f"{REDIS_PREFIX}{tag}"
            pipe.sadd(tag_full_key, blob_full_key)
            pipe.expire(tag_full_key, tag_ttl)
        await pipe.execute()
        return True
    except Exception as e:
        logger.error(f"Error storing in cache with tags: {str(e)}")
        return False


__all__ = [
    "extract_cache_tags",
    "store_in_cache_with_tags",
]
