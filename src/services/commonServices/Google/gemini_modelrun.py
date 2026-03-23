# from src.services.utils.unified_token_validator import validate_gemini_token_limit
from globals import logger
from src.exceptions import ApiCallError

from ..api_executor import execute_api_call
from google import genai
import traceback

async def gemini_modelrun_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for Gemini generate_content_stream."""
    client = genai.Client(api_key=apiKey)
    accumulated_text = ""
    accumulated_tool_calls = []
    usage = {}
    finish_reason = None
    try:
        async for chunk in await client.aio.models.generate_content_stream(**configuration):
            chunk_dict = chunk.model_dump()
            candidates = chunk_dict.get("candidates", [])
            usage_meta = chunk_dict.get("usage_metadata")
            if usage_meta:
                usage = {
                    "prompt_token_count": usage_meta.get("prompt_token_count", 0),
                    "candidates_token_count": usage_meta.get("candidates_token_count", 0),
                    "total_token_count": usage_meta.get("total_token_count", 0),
                    "cached_content_token_count": usage_meta.get("cached_content_token_count", 0),
                    "thoughts_token_count": usage_meta.get("thoughts_token_count", 0),
                }
            if not candidates:
                continue
            candidate = candidates[0]
            finish_reason = candidate.get("finish_reason") or finish_reason
            parts = candidate.get("content", {}).get("parts", []) or []
            for part in parts:
                if part.get("text"):
                    accumulated_text += part["text"]
                    yield {"content": part["text"], "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
                if part.get("function_call"):
                    fc = part["function_call"]
                    accumulated_tool_calls.append(fc)
        yield {"content": None, "tool_calls": accumulated_tool_calls or None, "usage": usage, "finish_reason": finish_reason, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def gemini_modelrun(
    configuration,
    apiKey,
    execution_time_logs,
    bridge_id,
    timer,
    message_id=None,
    org_id=None,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
):
    try:
        # Validate token count before making API call
        # model_name = configuration.get('model')
        # validate_gemini_token_limit(configuration, model_name, service, apiKey)
        
        client = genai.Client(api_key=apiKey) 

        # Define the API call function
        async def api_call(config):
            try:
                chat_completion = await client.aio.models.generate_content(**config)
                return {'success': True, 'response': chat_completion.model_dump()}
            except Exception as error:
                return {"success": False, "error": str(error), "status_code": getattr(error, "code", None)}

        # Execute API call with monitoring
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=True,
            name=name,
            org_name=org_name,
            service=service,
            count=count,
            token_calculator=token_calculator,
        )

    except Exception as error:
        execution_time_logs.append(
            {
                "step": f"{service} Processing time for call :- {count + 1}",
                "time_taken": timer.stop("API chat completion"),
            }
        )
        raise ApiCallError(str(error), status_code=getattr(error, "status_code", None), service=service) from error
