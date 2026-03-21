import traceback

from openai import AsyncOpenAI

# from src.services.utils.unified_token_validator import validate_openai_token_limit
from globals import logger
from src.exceptions import ApiCallError

from ..api_executor import execute_api_call


async def openrouter_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for OpenRouter chat.completions."""
    openAI = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=apiKey)
    config = {**configuration, "stream": True}
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        stream = await openAI.chat.completions.create(**config)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if chunk.usage:
                usage = chunk.usage.to_dict() if hasattr(chunk.usage, "to_dict") else dict(chunk.usage)
            if not choice:
                continue
            delta = choice.delta
            finish_reason = choice.finish_reason or finish_reason
            if delta.content:
                yield {"content": delta.content, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"id": tc.id or "", "name": tc.function.name or "", "arguments": ""}
                    if tc.function.arguments:
                        accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
        tool_calls_list = [
            {"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in accumulated_tool_calls.values()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": finish_reason, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def openrouter_modelrun(
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
        # Validate token count before making API call (raises exception if invalid)
        # model_name = configuration.get('model')
        # validate_openai_token_limit(configuration, model_name, service)

        openAI = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=apiKey)

        # Define the API call function
        async def api_call(config):
            try:
                chat_completion = await openAI.chat.completions.create(**config)
                return {"success": True, "response": chat_completion.to_dict()}
            except Exception as error:
                return {"success": False, "error": str(error), "status_code": getattr(error, "status_code", None)}

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
