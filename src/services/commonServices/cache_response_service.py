from fastapi.responses import JSONResponse
from globals import logger

from src.services.utils.common_utils import (
    create_latency_object,
    process_background_tasks,
    update_usage_metrics,
)

from .baseService.utils import sendResponse
from .cache_response_utils import (
    schedule_frequency_update,
    build_cache_params,
    build_cached_result,
    build_history_params,
    lookup_cached_payload,
)


async def try_handle_cached_response(
    parsed_data,
    timer,
    thread_info,
    transfer_request_id,
    bridge_configurations,
):
    cached_payload = await lookup_cached_payload(parsed_data)

    if not cached_payload.get("found"):
        logger.debug("Cache miss; continuing with normal LLM flow")
        return None

    parsed_data["is_cache_hit"] = True
    cached_answer = cached_payload.get("answer", "")
    cache_resource_id = cached_payload.get("resource_id")
    cache_score = cached_payload.get("score", 0.0)

    result = build_cached_result(parsed_data, cached_answer, cache_resource_id, cache_score)
    result["historyParams"] = build_history_params(parsed_data, result)

    parsed_data["alert_flag"] = False
    parsed_data["tokens"] = {"inputTokens": 0, "outputTokens": 0, "total_cost": 0}
    cache_params = build_cache_params(parsed_data)

    latency = create_latency_object(timer, cache_params)
    update_usage_metrics(parsed_data, cache_params, latency, result=result, success=True)
    result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

    logger.info(
        f"Sending cached response: resource_id={cache_resource_id}, score={cache_score:.1%}"
    )
    await sendResponse(
        parsed_data["response_format"],
        result["response"],
        success=True,
        variables=parsed_data.get("variables", {}),
    )
    await process_background_tasks(
        parsed_data, result, cache_params, thread_info, transfer_request_id, bridge_configurations
    )

    schedule_frequency_update(cache_resource_id)

    return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})
