"""Generic runner for OpenAI-Chat-Completions-compatible services.

Any service whose registry entry has ``client == "openai_sdk"`` and
``wire_format == "openai_chat"`` is dispatched here instead of having its own
copy-pasted ``*_modelrun.py``. The base URL and per-service capability flags
(stream usage, reasoning deltas) are read from the service registry, so adding
a new such service is a single DB insert — no new code.

This is a behavior-preserving consolidation of the previous moonshot / neevcloud
/ openrouter / openai_completion runners:
- base_url           -> service_registry.base_url(service)
- stream_options     -> emitted only when supports_stream_usage(service)
- reasoning deltas   -> accumulated only when supports_reasoning(service)
"""

from openai import AsyncOpenAI

from src.configs.service_registry import base_url, supports_reasoning, supports_stream_usage
from src.exceptions import ApiCallError
from src.services.utils.responses_api_adapter import function_tool_names, to_chat_completion, to_responses_request
from src.services.utils.web_search_config import has_web_search_tool, web_search_endpoint

from ..api_executor import execute_api_call


def _routes_web_search_to_responses(service, configuration):
    """The request carries the service's web search tool and the DB says that provider only serves it on its
    Responses API (services.web_search_tool.endpoint, e.g. MiniMax)."""
    return bool(web_search_endpoint(service)) and has_web_search_tool(service, configuration)


async def _web_search_via_responses(client, configuration):
    """Server-side web_search is only offered on the provider's Responses endpoint (e.g. MiniMax);
    run the request there and return it in chat-completions shape."""
    response = await client.responses.create(**to_responses_request(configuration))
    return to_chat_completion(response.to_dict(), function_names=function_tool_names(configuration))


async def openai_compatible_stream(configuration, apiKey, service):
    """Async generator yielding normalised delta dicts for any openai_sdk service."""
    client = AsyncOpenAI(base_url=base_url(service), api_key=apiKey)
    if _routes_web_search_to_responses(service, configuration):
        # Web search runs on the Responses API; deliver its result as a single chunk
        try:
            result = await _web_search_via_responses(client, configuration)
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
    config = {**configuration}
    if supports_stream_usage(service):
        config["stream_options"] = {"include_usage": True}
    emit_reasoning = supports_reasoning(service)
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        stream = await client.chat.completions.create(**config)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if chunk.usage:
                usage = chunk.usage.to_dict() if hasattr(chunk.usage, "to_dict") else dict(chunk.usage)
            if not choice:
                continue
            delta = choice.delta
            finish_reason = choice.finish_reason or finish_reason
            if emit_reasoning:
                reasoning_delta = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning_delta:
                    yield {"content": None, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": reasoning_delta}
            if delta.content:
                content = delta.content
                yield {"content": content, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
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


async def openai_compatible_modelrun(
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
    is_embed=None,
    user_id=None,
    thread_id=None,
    api_collection=None,
):
    try:
        client = AsyncOpenAI(base_url=base_url(service), api_key=apiKey)

        async def api_call(config):
            try:
                if _routes_web_search_to_responses(service, config):
                    response = await _web_search_via_responses(client, config)
                    if token_calculator and response.get("web_search_count"):
                        token_calculator.add_web_search_calls(response["web_search_count"])
                    return {"success": True, "response": response}
                chat_completion = await client.chat.completions.create(**config)
                return {"success": True, "response": chat_completion.to_dict()}
            except Exception as error:
                return {"success": False, "error": str(error), "status_code": getattr(error, "status_code", None)}

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
            is_embed=is_embed,
            user_id=user_id,
            thread_id=thread_id,
            api_collection=api_collection,
        )

    except Exception as error:
        execution_time_logs.append(
            {
                "step": f"{service} Processing time for call :- {count + 1}",
                "time_taken": timer.stop("API chat completion"),
            }
        )
        raise ApiCallError(str(error), status_code=getattr(error, "status_code", None), service=service) from error
