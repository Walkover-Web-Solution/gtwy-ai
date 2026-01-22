import json
import traceback
import uuid
from datetime import datetime

from globals import logger
from models.index import combined_models

# from src.services.utils.send_error_webhook import send_error_to_webhook
from src.configs.constant import redis_keys

from ..controllers.conversationController import savehistory_consolidated
from ..services.cache_service import find_in_cache, store_in_cache
from .conversationDbService import createOrchestratorConversationLog, timescale_metrics

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


async def create(dataset, history_params, version_id, thread_info=None):
    if thread_info is None:
        thread_info = {}
    try:
        conversations = []
        if thread_info is not None:
            thread_id = thread_info.get("thread_id")
            sub_thread_id = thread_info.get("sub_thread_id")
            conversations = thread_info.get("result", [])

        response = history_params.get("response", {})

        # Prepare consolidated conversation log data
        data_object = dataset[0]

        # Parse latency JSON to extract over_all_time
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
            "user": history_params.get("user", ""),
            "chatbot_message": history_params.get("chatbot_message", ""),
            "updated_llm_message": None,
            "error": str(data_object.get("error", "")) if not data_object.get("success", False) else None,
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
        }

        # Save consolidated conversation log
        await savehistory_consolidated(conversation_log_data)

        # Save conversations to Redis with TTL of 30 days
        if "error" not in dataset[0] and conversations:
            await save_conversations_to_redis(conversations, version_id, thread_id, sub_thread_id, history_params)

        # Extract latency for metrics (use already parsed latency_data)
        latency = latency_data.get("over_all_time", 0) if latency_data else 0

        # Only create metrics data if dataset has valid data
        metrics_data = []
        for data_object in dataset:
            # Skip empty data objects
            if not data_object or not data_object.get("orgId"):
                continue

            service = data_object.get("service", history_params.get("service", ""))
            metrics_data.append(
                {
                    "org_id": data_object.get("orgId", history_params.get("org_id")),
                    "bridge_id": history_params.get("bridge_id", ""),
                    "version_id": version_id,
                    "thread_id": history_params.get("thread_id", ""),
                    "model": data_object.get("model", history_params.get("model", "")),
                    "input_tokens": data_object.get("inputTokens", 0) or 0.0,
                    "output_tokens": data_object.get("outputTokens", 0) or 0.0,
                    "total_tokens": data_object.get("total_tokens", 0) or 0.0,
                    "apikey_id": data_object.get("apikey_object_id", {}).get(service, "")
                    if data_object.get("apikey_object_id")
                    else "",
                    "created_at": datetime.now(),
                    "latency": latency,
                    "success": data_object.get("success", False),
                    "cost": data_object.get("expectedCost", 0) or 0.0,
                    "time_zone": "Asia/Kolkata",
                    "service": service,
                }
            )

        # Create the cache key based on bridge_id (assuming it's always available)
        cache_key = f"{redis_keys['metrix_bridges_']}{history_params['bridge_id']}"
        # Safely load the old total token value from the cache
        cache_value = await find_in_cache(cache_key)
        try:
            oldTotalToken = json.loads(cache_value) if cache_value else 0
        except (json.JSONDecodeError, TypeError):
            oldTotalToken = 0

        # Calculate the total token sum, using .get() for 'totalTokens' to handle missing keys
        totaltoken = sum(data_object.get("total_tokens", 0) for data_object in dataset) + oldTotalToken
        # await send_error_to_webhook(history_params['bridge_id'], history_params['org_id'],totaltoken , 'metrix_limit_reached')
        await store_in_cache(cache_key, float(totaltoken))

        # Only save metrics if there's valid data
        if metrics_data:
            await timescale_metrics(metrics_data)
    except Exception as error:
        logger.error(f"Error during bulk insert of Ai middleware, {str(error)}")


async def create_orchestrator(transfer_chain, thread_info=None):
    """
    Create orchestrator conversation log entry by aggregating data from multiple agents.
    Each field will be a dictionary keyed by bridge_id.

    Args:
        transfer_chain: List of history entries from TRANSFER_HISTORY, each containing:
            - bridge_id: The bridge_id for this agent
            - history_params: History parameters for this agent
            - dataset: Usage data for this agent
            - version_id: Version ID for this agent
            - thread_info: Thread information
        thread_info: Thread information dict containing thread_id and sub_thread_id

    Returns:
        Integer ID of created record or None if failed
    """
    if thread_info is None:
        thread_info = {}
    try:
        if not transfer_chain or len(transfer_chain) == 0:
            logger.warning("Empty transfer chain provided to create_orchestrator")
            return None

        thread_id = thread_info.get("thread_id") if thread_info else None
        sub_thread_id = thread_info.get("sub_thread_id") if thread_info else None

        # Initialize aggregated data structures
        aggregated_data = {
            "llm_message": {},
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

        # Common fields (same for all agents)
        org_id = None
        service = None

        # Aggregate data from all agents in the transfer chain
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

            # Extract common fields from first valid entry
            if org_id is None:
                org_id = data_object.get("orgId") or history_params.get("org_id")
            if service is None:
                service = data_object.get("service") or history_params.get("service")

            # Parse latency JSON
            latency_data = {}
            try:
                if isinstance(data_object.get("latency"), str):
                    latency_data = json.loads(data_object.get("latency", "{}"))
                else:
                    latency_data = data_object.get("latency", {})
            except Exception:
                latency_data = {}

            response = history_params.get("response", {})

            # Aggregate data by bridge_id
            aggregated_data["llm_message"][bridge_id] = history_params.get("message", "")
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

            # Handle user_urls and urls as arrays of objects
            user_urls = history_params.get("user_urls", [])
            if user_urls:
                aggregated_data["user_urls"].append({bridge_id: user_urls})

            urls = history_params.get("llm_urls", [])
            if urls:
                aggregated_data["llm_urls"].append({bridge_id: urls})

            # Add bridge_id to agents_path
            if bridge_id not in aggregated_data["agents_path"]:
                aggregated_data["agents_path"].append(bridge_id)

        # Get thread_id and sub_thread_id from first entry if not in thread_info
        if not thread_id and transfer_chain:
            first_history_params = transfer_chain[0].get("history_params", {})
            thread_id = first_history_params.get("thread_id")
            sub_thread_id = first_history_params.get("sub_thread_id") or thread_id

        if not thread_id:
            logger.error("Missing thread_id in orchestrator history")
            return None

        if not sub_thread_id:
            sub_thread_id = thread_id

        # Prepare orchestrator conversation log data
        orchestrator_log_data = {
            "llm_message": aggregated_data["llm_message"],
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

        # Save orchestrator conversation log
        result_id = await createOrchestratorConversationLog(orchestrator_log_data)
        print(result_id)
        return result_id

    except Exception as error:
        logger.error(f"Error during orchestrator conversation log creation: {str(error)}")
        logger.error(traceback.format_exc())
        return None


# Exporting functions
__all__ = ["create", "create_orchestrator"]
