import traceback
from datetime import datetime

from globals import logger

from ..configs.constant import bridge_ids
from ..db_services import conversationDbService as chatbotDbService
from ..db_services.ConfigurationServices import save_sub_thread_id
from ..services.cache_service import find_in_cache, store_in_cache
from ..services.commonServices.baseService.utils import sendResponse
from ..services.utils.ai_call_util import call_ai_middleware


async def getThread(thread_id, sub_thread_id, org_id, bridge_id):
    try:
        # Get conversations from consolidated table with bridge_id filter
        chats = await chatbotDbService.find_conversation_logs(org_id, thread_id, sub_thread_id, bridge_id)
        chats = await add_tool_call_data_in_history(chats)

        return chats
    except Exception as err:
        logger.error(f"Error in getting thread:, {str(err)}, {traceback.format_exc()}")
        raise err


async def add_tool_call_data_in_history(chats):
    tools_call_indices = []

    for i in range(len(chats)):
        current_chat = chats[i]
        if current_chat["role"] == "tools_call":
            if i > 0 and (i + 1) < len(chats):
                prev_chat = chats[i - 1]
                next_chat = chats[i + 1]
                if prev_chat["role"] == "user" and next_chat["role"] == "assistant":
                    tools_call_data = current_chat.get("tools_call_data", [])
                    messages = []
                    for call_data in tools_call_data:
                        call_info = next(iter(call_data.values()))
                        name = call_info.get("name", "")
                        messages.append(f"{name}")
                    if messages:
                        combined_message = "tool_call has been done function name:-  " + ", ".join(messages)
                        if next_chat["content"]:
                            next_chat["content"] += "\n" + combined_message
                    tools_call_indices.append(i)

    processed_chats = [chat for idx, chat in enumerate(chats) if idx not in tools_call_indices]
    return processed_chats


async def save_sub_thread_id_and_name(thread_id, sub_thread_id, org_id, thread_flag, response_format, bridge_id, user):
    try:
        # Create Redis cache key for the combination
        cache_key = f"sub_thread_{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}"

        # Check if already exists in Redis cache
        cached_result = await find_in_cache(cache_key)
        if cached_result:
            logger.info(f"Found cached sub_thread_id for key: {cache_key}")
            return

        variables = {"user": user}
        display_name = sub_thread_id
        message = "generate description"
        current_time = datetime.now()
        if thread_flag:
            display_name = await call_ai_middleware(
                message, bridge_ids["generate_description"], response_type="text", variables=variables
            )
        await save_sub_thread_id(org_id, thread_id, sub_thread_id, display_name, bridge_id, current_time)

        # Store in Redis cache for 48 hours (172800 seconds)
        cache_data = {
            "org_id": org_id,
            "bridge_id": bridge_id,
            "thread_id": thread_id,
            "sub_thread_id": sub_thread_id,
            "display_name": display_name,
            "created_at": current_time.isoformat(),
        }
        await store_in_cache(cache_key, cache_data, ttl=172800)  # 48 hours

        if display_name is not None and display_name != sub_thread_id:
            response = {
                "data": {
                    "display_name": display_name,
                    "sub_thread_id": sub_thread_id,
                    "thread_id": thread_id,
                    "bridge_id": bridge_id,
                    "created_at": current_time.isoformat(),
                }
            }
            await sendResponse(response_format, response, True)

    except Exception as err:
        logger.error(f"Error in saving sub thread id and name:, {str(err)}")
        return {"success": False, "message": str(err)}


# Exporting the functions
__all__ = ["getThread", "save_sub_thread_id_and_name"]
