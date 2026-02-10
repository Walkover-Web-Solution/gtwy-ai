import uuid

from globals import logger
from src.configs.constant import bridge_ids
from src.services.commonServices.createConversations import ConversationService
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_service

from ..utils.ai_call_util import call_ai_middleware
from .baseService.utils import sendResponse


async def chatbot_suggestions(
    response_format, assistant, user, bridge_summary, thread_id, sub_thread_id, configuration, org_id
):
    try:
        prompt_summary = bridge_summary
        prompt = configuration["prompt"]
        conversation = ConversationService.createOpenAiConversation(
            configuration.get("conversation", {}), None, []
        ).get("messages", [])
        if conversation is None:
            conversation = []
        conversation.extend(
            [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant.get("data", "").get("content")},
            ]
        )
        final_prompt = prompt_summary if prompt_summary is not None else prompt
        random_id = str(uuid.uuid4())
        updated_prompt = await get_specific_prebuilt_prompt_service(org_id, "chatbot_suggestions")
        configuration = None
        if updated_prompt and updated_prompt.get("chatbot_suggestions"):
            configuration = {
                "prompt": updated_prompt.get("chatbot_suggestions"),
            }
        message = (
            f"Generate suggestions based on the user conversations. \n **User Conversations**: {conversation[-2:]}"
        )
        variables = {"prompt_summary": final_prompt}
        thread_id = f"{thread_id or random_id}-{sub_thread_id or random_id}"
        result = await call_ai_middleware(
            message,
            bridge_id=bridge_ids["chatbot_suggestions"],
            configuration=configuration,
            variables=variables,
            thread_id=thread_id,
        )
        response = {"data": {"suggestions": result["suggestions"]}}
        await sendResponse(response_format, response, success=True)

    except Exception as err:
        logger.error(f"Error calling function chatbot_suggestions =>, {str(err)}")
