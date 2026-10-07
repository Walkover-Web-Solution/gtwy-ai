import json

from src.exceptions import ApiCallError
from src.services.utils.responses_api_adapter import function_tool_names, to_chat_completion, to_responses_request
from src.services.utils.web_search_config import has_web_search_tool, web_search_endpoint

from ...utils.apiservice import fetch, fetch_stream
from ..api_executor import execute_api_call


def _web_search_url(configuration):
    """xAI only serves web_search on its Responses API. The path comes from the grok service's
    web_search_tool.endpoint in the DB, joined to its base_url; None when this is not a web search call."""
    if not has_web_search_tool("grok", configuration):
        return None
    url = web_search_endpoint("grok")
    if not url:
        raise ValueError("grok web_search_tool.endpoint is not configured in the services collection")
    return url


async def _grok_web_search_call(url, configuration, headers):
    """Call xAI's Responses API and return a chat-completions shaped result."""
    response_data, _ = await fetch(url=url, method="POST", headers=headers, json_body=to_responses_request(configuration))
    return to_chat_completion(response_data, function_names=function_tool_names(configuration))


async def grok_stream(configuration, api_key):
    """Async generator yielding normalised delta dicts for xAI Grok."""
    url = "https://api.x.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        web_search_url = _web_search_url(configuration)
    except ValueError as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}
        return
    if web_search_url:
        # Web search runs through the Responses API; deliver its result as a single chunk
        try:
            result = await _grok_web_search_call(web_search_url, configuration, headers)
            message = result["choices"][0]["message"]
            if message.get("content"):
                yield {"content": message["content"], "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            yield {
                "content": None,
                "tool_calls": message.get("tool_calls"),
                "usage": result["usage"],
                "finish_reason": result["choices"][0]["finish_reason"],
                "reasoning": None,
            }
        except Exception as error:
            yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}
        return
    payload = {
        **configuration,
        "stream_options": {
            "include_usage": True
        },
    }
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        async for line in fetch_stream(url=url, headers=headers, json_body=payload):
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
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

            web_search_url = _web_search_url(config)
            if web_search_url:
                response_data = await _grok_web_search_call(web_search_url, config, headers)
                if token_calculator and response_data.get("web_search_count"):
                    token_calculator.add_web_search_calls(response_data["web_search_count"])
                return {"success": True, "response": response_data}

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
