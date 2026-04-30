import traceback

from globals import logger

from ..db_services import conversationDbService as chatbotDbService


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


__all__ = ["getThread"]
