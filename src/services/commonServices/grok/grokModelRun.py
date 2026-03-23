import json
import traceback

import httpx
from globals import logger
from src.exceptions import ApiCallError

from ...utils.apiservice import fetch
from ..api_executor import execute_api_call


async def grok_stream(configuration, api_key):
    """Async generator yielding normalised delta dicts for xAI Grok via httpx streaming."""
    url = "https://api.x.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        **configuration,
        "stream": True,
        "stream_options": {
            "include_usage": True
        },
    }
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        yield {"content": delta["content"], "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"id": tc.get("id", ""), "name": tc.get("function", {}).get("name", ""), "arguments": ""}
                            accumulated_tool_calls[idx]["arguments"] += tc.get("function", {}).get("arguments", "")
        tool_calls_list = [
            {"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in accumulated_tool_calls.values()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": finish_reason, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def grok_runmodel(
    configuration,
    api_key,
    execution_time_logs,
    bridge_id,
    timer,
    message_id,
    org_id,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
):
    """Execute a chat completion call against the xAI Grok API using custom fetch function."""

    async def api_call(config):
        try:
            # Prepare the request payload for xAI API
            url = "https://api.x.ai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            # Use the custom fetch function to make the API call
            response_data, response_headers = await fetch(url=url, method="POST", headers=headers, json_body=config)

            # Parse the response similar to OpenAI format
            return {"success": True, "response": response_data}
        except Exception as error:
            return {"success": False, "error": str(error), "status_code": getattr(error, "status_code", None)}

    try:
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=False,
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
