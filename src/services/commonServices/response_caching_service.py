from globals import logger
from src.services.utils.ai_middleware_format import Response_formatter
from src.configs.constant import service_name
from src.services.commonServices.baseService.utils import serialize_config

from .response_caching_utils import (
    get_memories_from_hippocampus,
    get_response_using_resourceid,
)


def _is_cache_eligible(parsed_data: dict) -> bool:
    return bool(
        parsed_data.get("cache_on")
        and parsed_data.get("chatbot_auto_answers", False)
        and parsed_data.get("configuration", {}).get("type") == "chat"
        and parsed_data.get("user")
        and not parsed_data.get("is_playground", False)
    )


async def handle_response_caching(parsed_data: dict, class_obj=None):
    if not _is_cache_eligible(parsed_data):
        result = await class_obj.execute()
        return result

    timer = class_obj.timer
    execution_logs = class_obj.execution_time_logs

    timer.start()
    memory_match = await get_memories_from_hippocampus(
        user_question=parsed_data.get("user", ""),
        agent_id=parsed_data.get("bridge_id", ""),
        top_k=1,
        limit=1,
        min_score=0.85,
    )
    
    execution_logs.append(
        {
            "step": "Getting memories from Hippocampus",
            "time_taken": timer.stop("Getting memories from Hippocampus"),
        }
    )

    resource_id = memory_match.get("resource_id")
    score = memory_match.get("score", 0.0)
    if memory_match.get("found") and resource_id:
        
        timer.start()
        mongo_response = await get_response_using_resourceid(resource_id)
       
        if mongo_response.get("found"):
            answer = mongo_response.get("answer") or ""
            logger.info(f"Cache hit: resource_id={resource_id}, score={score:.1%}")
            response = {
                "id": resource_id,
                "content": answer
            }
            response = await Response_formatter(response=answer,isCache=True)

            # Handling parsing error for Gemini 
            if class_obj.service == service_name["gemini"]:
                class_obj.customConfig = serialize_config(class_obj.customConfig)

            historyParams = class_obj.prepare_history_params(
                response=response,
                model_response={"data": [{}], "firstAttemptError": ""},
                tools={},
                is_cached=True
            )
            result = {'success': True, 'modelResponse': {}, 'historyParams': historyParams, 'response': response}
            parsed_data["is_cache_hit"] = True
            parsed_data["cache_hit_resource_id"] = resource_id

            execution_logs.append(
                {
                    "step": "Getting Response using ResourceID",
                    "time_taken": timer.stop(f"Found for ResourceID: {resource_id}"),
                }
            )
            return result
        
        execution_logs.append(
            {
                "step": "Getting Response using ResourceID",
                "time_taken": timer.stop(f"Not Found For ResourceID: {resource_id}"),
            }
        )

    result = await class_obj.execute()
    return result
