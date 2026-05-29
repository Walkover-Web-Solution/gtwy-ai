import json
import traceback
import uuid
from datetime import datetime

from globals import logger
from models.index import combined_models

# from src.services.utils.send_error_webhook import send_error_to_webhook
from src.configs.constant import redis_keys

from ..services.cache_service import store_in_cache

postgres = combined_models["pg"]
timescale = combined_models["timescale"]


async def save_conversations_to_redis(conversations, version_id, thread_id, sub_thread_id, history_params):
    """
    Save conversations to Redis with conversation management logic.
    If conversation array has more than 9 items, remove first 2 and add current conversation.
    """
    try:
        # Create Redis key
        redis_key = f"{redis_keys['conversation_']}{version_id}_{thread_id}_{sub_thread_id}"
        # Start with existing conversations from database
        conversation_list = conversations or []

        # Create current conversation entries (user + assistant)
        current_time = datetime.now().isoformat() + "+00:00"

        # User message
        user_conversation = {
            "content": history_params["user"],
            "role": "user",
            "createdAt": current_time,
            "id": int(str(uuid.uuid4().int)[:8]),  # Generate 8-digit ID
            "function": None,
            "is_reset": False,
            "tools_call_data": history_params.get("tools_call_data"),
            "error": "",
            "urls": history_params.get("urls", []),
        }

        # Assistant message
        assistant_conversation = {
            "content": history_params.get("message", ""),
            "role": "assistant",
            "createdAt": current_time,
            "id": int(str(uuid.uuid4().int)[:8]),  # Generate 8-digit ID
            "function": {},
            "is_reset": False,
            "tools_call_data": None,
            "error": None,
            "urls": [],
        }

        # Add new conversations first
        conversation_list.extend([user_conversation, assistant_conversation])

        # Manage conversation array size - if more than 9, remove first 2
        if len(conversation_list) > 9:
            # Remove first 2 conversations
            conversation_list = conversation_list[2:]

        # Save to Redis with 30 days TTL (30 * 24 * 60 * 60 = 2592000 seconds)
        ttl_30_days = 2592000
        await store_in_cache(redis_key, conversation_list, ttl_30_days)

        logger.info(f"Saved conversations to Redis with key: {redis_key}")

    except Exception as error:
        logger.error(f"Error saving conversations to Redis: {str(error)}")
        logger.error(traceback.format_exc())


async def create_batch_conversation_logs(batch_id, messages, parsed_data, processed_prompts, batch_variables):
    """
    Build conversation log entries for each batch message and publish to the log queue
    for Node.js to save as a bulk INSERT.

    Args:
        batch_id: The batch ID from the provider
        messages: List of message mappings containing message, custom_id, and variables
        parsed_data: Parsed request data containing bridge_id, org_id, etc.
        processed_prompts: List of processed prompts for each batch message
        batch_variables: List of variables for each batch message (or None)
    """
    try:
        from ..services.utils.helper import Helper
        from ..services.cache_service import make_json_serializable
        from ..services.commonServices.queueService.queueLogService import sub_queue_obj

        batch_history_entries = []

        for idx, message_info in enumerate(messages):
            user_message = message_info.get("message", "")
            message_id = message_info.get("message_id", "")
            variables = message_info.get("variables", {}) if batch_variables else {}

            webhook_info = parsed_data.get('batch_webhook') or {}
            masked_headers = Helper.mask_headers(webhook_info.get('headers'))

            conversation_log_data = {
                "user": user_message,
                "llm_message": "Your message has been queued for batch processing. You will receive the response shortly.",
                "chatbot_message": None,
                "bridge_id": parsed_data.get('bridge_id'),
                "org_id": parsed_data.get('org_id'),
                "thread_id": parsed_data.get('thread_id'),
                "sub_thread_id": parsed_data.get('sub_thread_id'),
                "version_id": parsed_data.get('version_id', ''),
                "AiConfig": parsed_data.get('AiConfig') or {},
                "service": parsed_data.get('service'),
                "model": parsed_data.get('model'),
                "status": False,
                "variables": variables,
                "message_id": message_id,
                "prompt": processed_prompts[idx] if idx < len(processed_prompts) else None,
                "batch_data": {
                    "status": "queued",
                    "batch_id": batch_id,
                    "webhook_url": webhook_info.get('url'),
                    "webhook_headers": masked_headers
                }
            }
            batch_history_entries.append(conversation_log_data)

        if batch_history_entries:
            batch_history_entries[0]["thread_flag"] = parsed_data.get("thread_flag")
            batch_history_entries[0]["response_format"] = parsed_data.get("response_format")
            message = make_json_serializable({"save_batch_history": batch_history_entries})
            await sub_queue_obj.publish_message(message)

        logger.info(f"Published {len(batch_history_entries)} batch conversation logs for batch {batch_id} to queue")

    except Exception as error:
        logger.error(f'Error publishing batch conversation logs: {str(error)}')
        logger.error(traceback.format_exc())

def build_history_and_metrics_payload(dataset, history_params, version_id):
    """
    Build conversation log and metrics payload without saving to DB.
    Used to prepare data for publishing to the log queue for Node.js to save.

    Returns:
        dict with keys: conversation_log_data, metrics_data, total_tokens, bridge_id
    """
    response = history_params.get("response", {})
    data_object = dataset[0]

    latency_data = {}
    try:
        if isinstance(data_object.get("latency"), str):
            latency_data = json.loads(data_object.get("latency", "{}"))
        else:
            latency_data = data_object.get("latency", {})
    except Exception:
        latency_data = {}

    conversation_log_data = {
        "llm_message": history_params.get("message", ""),
        "reasoning": history_params.get("reasoning", ""),
        "user": history_params.get("user", ""),
        "chatbot_message": history_params.get("chatbot_message", ""),
        "updated_llm_message": None,
        "error": str(data_object.get("error", "")) if not data_object.get("success", False) else (history_params.get("error") or None),
        "user_feedback": 0,
        "tools_call_data": history_params.get("tools_call_data", []),
        "message_id": str(history_params.get("message_id")),
        "sub_thread_id": history_params.get("sub_thread_id"),
        "thread_id": history_params.get("thread_id"),
        "version_id": version_id,
        "user_urls": history_params.get("user_urls", []),
        "llm_urls": history_params.get("llm_urls", []),
        "AiConfig": history_params.get("AiConfig"),
        "fallback_model": history_params.get("fallback_model") or {},
        "org_id": data_object.get("orgId") or history_params.get("org_id"),
        "service": data_object.get("service") or history_params.get("service"),
        "model": data_object.get("model") or history_params.get("model"),
        "status": data_object.get("success", False),
        "tokens": {
            "input_tokens": data_object.get("inputTokens", 0),
            "output_tokens": data_object.get("outputTokens", 0),
            "expected_cost": data_object.get("expectedCost", 0),
        },
        "variables": data_object.get("variables") or {},
        "latency": latency_data,
        "firstAttemptError": history_params.get("firstAttemptError"),
        "finish_reason": response.get("data", {}).get("finish_reason"),
        "parent_id": history_params.get("parent_id") or "",
        "child_id": history_params.get("child_id"),
        "bridge_id": history_params.get("bridge_id"),
        "prompt": history_params.get("prompt"),
        "is_cached": history_params.get("is_cached", False),
        "plans": history_params.get("plans"),
        "testcase_id": history_params.get("testcase_id"),
        "testcase_data": history_params.get("testcase_data"),
    }

    latency = latency_data.get("over_all_time", 0) if latency_data else 0

    metrics_data = []
    for data_obj in dataset:
        if not data_obj or not data_obj.get("orgId"):
            continue
        service = data_obj.get("service", history_params.get("service", ""))
        metrics_data.append(
            {
                "org_id": data_obj.get("orgId", history_params.get("org_id")),
                "bridge_id": history_params.get("bridge_id", ""),
                "version_id": version_id,
                "thread_id": history_params.get("thread_id", ""),
                "model": data_obj.get("model", history_params.get("model", "")),
                "input_tokens": data_obj.get("inputTokens", 0) or 0.0,
                "output_tokens": data_obj.get("outputTokens", 0) or 0.0,
                "total_tokens": data_obj.get("total_tokens", 0) or 0.0,
                "apikey_id": data_obj.get("apikey_object_id", {}).get(service, "")
                if data_obj.get("apikey_object_id")
                else "",
                "latency": latency,
                "success": data_obj.get("success", False),
                "cost": data_obj.get("expectedCost", 0) or 0.0,
                "time_zone": "Asia/Kolkata",
                "service": service,
            }
        )

    total_tokens = sum(d.get("total_tokens", 0) for d in dataset)

    return {
        "conversation_log_data": conversation_log_data,
        "metrics_data": metrics_data,
        "total_tokens": total_tokens,
        "bridge_id": history_params.get("bridge_id"),
    }


def build_orchestrator_log_data(transfer_chain, thread_info=None):
    """
    Build orchestrator conversation log data from a transfer chain without saving to DB.
    Used to prepare data for publishing to the log queue for Node.js to save.

    Returns:
        dict with orchestrator_log_data ready for Node.js to insert, or None if failed
    """
    if thread_info is None:
        thread_info = {}

    if not transfer_chain or len(transfer_chain) == 0:
        logger.warning("Empty transfer chain provided to build_orchestrator_log_data")
        return None

    thread_id = thread_info.get("thread_id") if thread_info else None
    sub_thread_id = thread_info.get("sub_thread_id") if thread_info else None

    aggregated_data = {
        "llm_message": {},
        "reasoning": {},
        "user": {},
        "chatbot_message": {},
        "updated_llm_message": {},
        "prompt": {},
        "error": {},
        "tools_call_data": {},
        "message_id": {},
        "version_id": {},
        "bridge_id": {},
        "user_urls": [],
        "llm_urls": [],
        "AiConfig": {},
        "fallback_model": {},
        "model": {},
        "status": {},
        "tokens": {},
        "variables": {},
        "latency": {},
        "firstAttemptError": {},
        "finish_reason": {},
        "agents_path": [],
    }

    org_id = None
    service = None

    for history_entry in transfer_chain:
        bridge_id = history_entry.get("bridge_id")
        if not bridge_id:
            continue

        history_params = history_entry.get("history_params", {})
        dataset = history_entry.get("dataset", [{}])
        version_id = history_entry.get("version_id")

        if not dataset or len(dataset) == 0:
            continue

        data_object = dataset[0]

        if org_id is None:
            org_id = data_object.get("orgId") or history_params.get("org_id")
        if service is None:
            service = data_object.get("service") or history_params.get("service")

        latency_data = {}
        try:
            if isinstance(data_object.get("latency"), str):
                latency_data = json.loads(data_object.get("latency", "{}"))
            else:
                latency_data = data_object.get("latency", {})
        except Exception:
            latency_data = {}

        response = history_params.get("response", {})

        aggregated_data["llm_message"][bridge_id] = history_params.get("message", "")
        aggregated_data["reasoning"][bridge_id] = history_params.get("reasoning", "")
        aggregated_data["user"][bridge_id] = history_params.get("user", "")
        aggregated_data["chatbot_message"][bridge_id] = history_params.get("chatbot_message", "")
        aggregated_data["updated_llm_message"][bridge_id] = None
        aggregated_data["prompt"][bridge_id] = history_params.get("prompt")
        aggregated_data["error"][bridge_id] = (
            str(data_object.get("error", "")) if not data_object.get("success", False) else None
        )
        aggregated_data["tools_call_data"][bridge_id] = history_params.get("tools_call_data", [])
        aggregated_data["message_id"][bridge_id] = str(history_params.get("message_id", ""))
        aggregated_data["version_id"][bridge_id] = version_id
        aggregated_data["bridge_id"][bridge_id] = bridge_id
        aggregated_data["model"][bridge_id] = data_object.get("model") or history_params.get("model")
        aggregated_data["status"][bridge_id] = data_object.get("success", False)
        aggregated_data["tokens"][bridge_id] = {
            "input": data_object.get("inputTokens", 0),
            "output": data_object.get("outputTokens", 0),
            "expected_cost": data_object.get("expectedCost", 0),
        }
        aggregated_data["variables"][bridge_id] = data_object.get("variables") or {}
        aggregated_data["latency"][bridge_id] = latency_data.get("over_all_time", 0) if latency_data else 0
        aggregated_data["firstAttemptError"][bridge_id] = history_params.get("firstAttemptError")
        aggregated_data["finish_reason"][bridge_id] = response.get("data", {}).get("finish_reason")
        aggregated_data["AiConfig"][bridge_id] = history_params.get("AiConfig")
        aggregated_data["fallback_model"][bridge_id] = history_params.get("fallback_model") or {}

        user_urls = history_params.get("user_urls", [])
        if user_urls:
            aggregated_data["user_urls"].append({bridge_id: user_urls})

        urls = history_params.get("llm_urls", [])
        if urls:
            aggregated_data["llm_urls"].append({bridge_id: urls})

        if bridge_id not in aggregated_data["agents_path"]:
            aggregated_data["agents_path"].append(bridge_id)

    if not thread_id and transfer_chain:
        first_history_params = transfer_chain[0].get("history_params", {})
        thread_id = first_history_params.get("thread_id")
        sub_thread_id = first_history_params.get("sub_thread_id") or thread_id

    if not thread_id:
        logger.error("Missing thread_id in build_orchestrator_log_data")
        return None

    if not sub_thread_id:
        sub_thread_id = thread_id

    return {
        "llm_message": aggregated_data["llm_message"],
        "reasoning": aggregated_data["reasoning"],
        "user": aggregated_data["user"],
        "chatbot_message": aggregated_data["chatbot_message"],
        "updated_llm_message": aggregated_data["updated_llm_message"],
        "prompt": aggregated_data["prompt"],
        "error": aggregated_data["error"],
        "tools_call_data": aggregated_data["tools_call_data"],
        "message_id": aggregated_data["message_id"],
        "sub_thread_id": sub_thread_id,
        "thread_id": thread_id,
        "version_id": aggregated_data["version_id"],
        "bridge_id": aggregated_data["bridge_id"],
        "user_urls": aggregated_data["user_urls"],
        "llm_urls": aggregated_data["llm_urls"],
        "AiConfig": aggregated_data["AiConfig"],
        "fallback_model": aggregated_data["fallback_model"],
        "org_id": org_id,
        "service": service,
        "model": aggregated_data["model"],
        "status": aggregated_data["status"],
        "tokens": aggregated_data["tokens"],
        "variables": aggregated_data["variables"],
        "latency": aggregated_data["latency"],
        "firstAttemptError": aggregated_data["firstAttemptError"],
        "finish_reason": aggregated_data["finish_reason"],
        "agents_path": aggregated_data["agents_path"],
    }


async def publish_plan_history_update(
    *,
    parsed_data,
    final_plan,
    main_agent_metrics=None,
    history_params_extra=None,
):
    """
    Publish an update_history message to the log queue so Node.js can update
    the existing conversation_logs row (identified by message_id) once plan
    execution is complete.

    The initial row was created during the planning phase with the full
    conversation-log shape but empty metrics.  Here we build the same full
    shape, enriched with aggregated main-agent telemetry (tokens, latency,
    reasoning, tools_call_data, etc.) collected by the executor.  The Node
    consumer whitelist-updates whatever fields it finds in the payload, so
    this is forward-compatible.

    Args:
        parsed_data: The request dict (has bridge_id, thread_id, sub_thread_id,
                     org_id, service, model, message_id, user, prompt, version_id).
        final_plan: The fully-executed plan dict with task results.
        main_agent_metrics: Output of `finalize_main_agent_metrics` from the
                            executor. None for planning-phase updates where no
                            execution happened.
        history_params_extra: Dict with override keys for final message /
                              finish_reason / status (e.g. completion copy).
    """
    try:
        from ..services.cache_service import make_json_serializable
        from ..services.commonServices.queueService.queueLogService import sub_queue_obj

        metrics = main_agent_metrics or {}
        extra = history_params_extra or {}

        message_id = (
            (final_plan or {}).get("message_id")
            or parsed_data.get("message_id")
        )
        if not message_id:
            logger.error("publish_plan_history_update: missing message_id — cannot update history row")
            return

        version_id = parsed_data.get("version_id")
        thread_id = parsed_data.get("thread_id")
        sub_thread_id = parsed_data.get("sub_thread_id") or thread_id

        # Synthesize the single-element `dataset` that build_history_and_metrics_payload expects.
        dataset = [{
            "orgId": parsed_data.get("org_id"),
            "service": metrics.get("service") or parsed_data.get("service"),
            "model": metrics.get("model") or parsed_data.get("model"),
            "success": extra.get("status", metrics.get("success", True)),
            "inputTokens": metrics.get("input_tokens", 0),
            "outputTokens": metrics.get("output_tokens", 0),
            "total_tokens": metrics.get("total_tokens", 0),
            "expectedCost": metrics.get("expected_cost", 0),
            "latency": metrics.get("latency") or {},
            "variables": parsed_data.get("variables") or {},
            "error": metrics.get("last_error"),
        }]

        history_params = {
            "message": extra.get("message", ""),
            "reasoning": metrics.get("reasoning", ""),
            "user": parsed_data.get("user", ""),
            "chatbot_message": "",
            "error": metrics.get("last_error"),
            "tools_call_data": metrics.get("tools_call_data", []),
            "message_id": message_id,
            "sub_thread_id": sub_thread_id,
            "thread_id": thread_id,
            "user_urls": parsed_data.get("user_urls", []),
            "llm_urls": metrics.get("llm_urls", []),
            "AiConfig": metrics.get("AiConfig"),
            "fallback_model": metrics.get("fallback_model") or {},
            "org_id": parsed_data.get("org_id"),
            "service": metrics.get("service") or parsed_data.get("service"),
            "model": metrics.get("model") or parsed_data.get("model"),
            "firstAttemptError": metrics.get("firstAttemptError"),
            "bridge_id": parsed_data.get("bridge_id"),
            "prompt": parsed_data.get("prompt"),
            "plans": final_plan,
            "response": {
                "data": {
                    "finish_reason": extra.get(
                        "finish_reason", metrics.get("finish_reason", "stop")
                    )
                }
            },
        }

        payload = build_history_and_metrics_payload(dataset, history_params, version_id)
        conversation_log_data = payload["conversation_log_data"]

        # build_history_and_metrics_payload derives `status` from dataset[0].success;
        # honor an explicit override if the caller provided one.
        if "status" in extra:
            conversation_log_data["status"] = extra["status"]

        # `plans` is always the authoritative executed plan.
        conversation_log_data["plans"] = final_plan

        update_data = {
            "message_id": str(message_id),
            **conversation_log_data,
        }
        message = make_json_serializable({"update_history": update_data})
        await sub_queue_obj.publish_message(message)
        logger.info(f"Published plan history update for message_id: {message_id}")

    except Exception as error:
        logger.error(f"Error publishing plan history update: {str(error)}")
        logger.error(traceback.format_exc())


# Exporting functions
__all__ = ["find", "find_one", "find_one_pg", "create_batch_conversation_logs", "build_history_and_metrics_payload", "build_orchestrator_log_data", "publish_plan_history_update"]
