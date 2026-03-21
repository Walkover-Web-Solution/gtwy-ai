import traceback

from mistralai import Mistral

from globals import logger
from src.exceptions import ApiCallError

from ..api_executor import execute_api_call


async def mistral_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for Mistral chat."""
    mistral = Mistral(api_key=apiKey)
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        stream = await mistral.chat.stream_async(**configuration)
        async for event in stream:
            choice = event.data.choices[0] if event.data.choices else None
            if event.data.usage:
                u = event.data.usage
                usage = u.model_dump() if hasattr(u, "model_dump") else vars(u)
            if not choice:
                continue
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta
            if delta.content:
                yield {"content": delta.content, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = getattr(tc, "index", len(accumulated_tool_calls))
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"id": getattr(tc, "id", "") or "", "name": tc.function.name or "", "arguments": ""}
                    if tc.function.arguments:
                        accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
        tool_calls_list = [
            {"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in accumulated_tool_calls.values()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": str(finish_reason) if finish_reason else None, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def mistral_model_run(
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
        mistral = Mistral(api_key=apiKey)

        # Define the API call function
        async def api_call(config):
            try:
                chat_completion = await mistral.chat.complete_async(**config)
                return {"success": True, "response": chat_completion.model_dump()}
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
