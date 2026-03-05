import asyncio

from globals import logger
from src.services.agent_memory_service import get_cached_agent_response, update_frequency_in_mongodb
from src.services.utils.ai_middleware_format import cached_response_formatter


async def lookup_cached_payload(parsed_data):
    try:
        return await get_cached_agent_response(
            user_question=parsed_data.get("user", ""),
            agent_id=parsed_data.get("bridge_id", ""),
        )
    except Exception as cache_error:
        logger.warning(f"Cache lookup errored; continuing with normal LLM flow: {cache_error}")

    return {"found": False, "answer": None, "score": 0.0, "resource_id": None}


def schedule_frequency_update(cache_resource_id):
    if cache_resource_id:
        asyncio.create_task(update_frequency_in_mongodb(cache_resource_id))


def build_cached_result(parsed_data, cached_answer, cache_resource_id, cache_score):
    return cached_response_formatter(
        cached_answer=cached_answer,
        cache_resource_id=cache_resource_id,
        cache_score=cache_score,
        message_id=parsed_data.get("message_id"),
    )


def build_history_params(parsed_data, result):
    return {
        "thread_id": parsed_data.get("thread_id"),
        "sub_thread_id": parsed_data.get("sub_thread_id"),
        "user": parsed_data.get("user") or "",
        "message": result.get("response", {}).get("data", {}).get("content", ""),
        "org_id": parsed_data.get("org_id"),
        "bridge_id": parsed_data.get("bridge_id"),
        "model": None,
        "service": parsed_data.get("service"),
        "channel": "chat",
        "type": "success",
        "actor": "assistant",
        "tools": {},
        "chatbot_message": "",
        "tools_call_data": [],
        "message_id": parsed_data.get("message_id"),
        "llm_urls": [],
        "revised_prompt": None,
        "user_urls": [
            *({"url": u, "type": "image"} for u in (parsed_data.get("images") or [])),
            *({"url": u, "type": "pdf"} for u in (parsed_data.get("files") or [])),
            *({"url": u, "type": "audio"} for u in (parsed_data.get("audios") or [])),
        ],
        "AiConfig": None,
        "firstAttemptError": "",
        "annotations": [],
        "fallback_model": "",
        "response": result.get("response"),
        "folder_id": parsed_data.get("folder_id"),
        "folder_limit": parsed_data.get("folder_limit", 0),
        "parent_id": parsed_data.get("parent_bridge_id", ""),
        "child_id": None,
        "prompt": parsed_data.get("configuration", {}).get("prompt"),
        "is_cached": True,
    }


def build_cache_params(parsed_data):
    return {
        "configuration": parsed_data.get("configuration", {}),
        "execution_time_logs": [],
        "function_time_logs": [],
        "apikey_object_id": parsed_data.get("apikey_object_id"),
    }
