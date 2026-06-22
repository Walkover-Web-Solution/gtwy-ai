import copy
import json
import traceback

from globals import logger
from src.exceptions import ApiCallError

from ...utils.apiservice import fetch, fetch_stream
from ..api_executor import execute_api_call

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


def _openai_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _parse_error_response(status_code, body_text):
    try:
        body = json.loads(body_text)
        msg = body.get("error", {}).get("message", body_text)
    except (json.JSONDecodeError, AttributeError):
        msg = body_text
    return f"Error code: {status_code} - {msg}"


def remove_duplicate_ids_from_input(configuration):
    """
    Remove duplicate items with same IDs from the input array to prevent OpenAI API errors
    """
    config_copy = copy.deepcopy(configuration)

    if "input" not in config_copy:
        return config_copy

    input_array = config_copy["input"]
    seen_ids = set()

    filtered_input = []

    for item in input_array:
        if isinstance(item, dict) and "id" in item:
            original_id = item["id"]
            if original_id in seen_ids:
                logger.info(f"Removing duplicate item with ID: {original_id}")
                continue
            else:
                seen_ids.add(original_id)
                filtered_input.append(item)
        else:
            filtered_input.append(item)

    config_copy["input"] = filtered_input

    return config_copy


async def openai_response_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for openai responses API."""
    headers = _openai_headers(apiKey)
    
    # Remove duplicate IDs from input before streaming
    cleaned_config = remove_duplicate_ids_from_input(configuration)
    payload = {**cleaned_config, "stream": True}
    
    accumulated_output = []
    accumulated_tool_calls = {}
    emitted_tool_call_ids = set()
    usage = {}
    finish_reason = None
    service_tier = None
    incomplete_details = None
    try:
        async for line in fetch_stream(url=OPENAI_RESPONSES_URL, headers=headers, json_body=payload):
            if line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # Opportunistically capture usage/status from any event carrying a response
            # snapshot with a populated usage object. The terminal event
            # (response.completed / response.incomplete / response.failed) always carries
            # the final usage, but the exact event-type string can vary by model, so we
            # key off the presence of usage rather than the type name. Intermediate
            # snapshots (response.created / in_progress) have usage=None and are skipped.
            resp_snapshot = event.get("response")
            if isinstance(resp_snapshot, dict) and resp_snapshot.get("usage"):
                usage = resp_snapshot["usage"]
                status = resp_snapshot.get("status")
                if status == "incomplete":
                    # Reasoning models commonly end here after exhausting their token
                    # budget; the meaningful reason lives in incomplete_details.reason.
                    incomplete_details = resp_snapshot.get("incomplete_details") or {}
                    finish_reason = incomplete_details.get("reason") or status
                elif status == "failed":
                    error_obj = resp_snapshot.get("error") or {}
                    finish_reason = error_obj.get("code") or "failed"
                elif status:
                    finish_reason = status
                response_output = resp_snapshot.get("output")
                if isinstance(response_output, list) and response_output:
                    accumulated_output = response_output
                if resp_snapshot.get("service_tier"):
                    service_tier = resp_snapshot["service_tier"]

            if event_type == "response.output_text.delta":
                yield {"content": event.get("delta"), "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            elif event_type == "response.reasoning_summary_text.delta":
                yield {"content": None, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": event.get("delta")}
            elif event_type == "response.function_call_arguments.delta":
                item_id = event.get("item_id", "")
                if item_id not in accumulated_tool_calls:
                    accumulated_tool_calls[item_id] = {"name": "", "call_id": "", "arguments": ""}
                accumulated_tool_calls[item_id]["arguments"] += event.get("delta", "")
            elif event_type == "response.function_call_arguments.done":
                item_id = event.get("item_id", "")
                if item_id not in accumulated_tool_calls:
                    accumulated_tool_calls[item_id] = {"name": "", "call_id": "", "arguments": ""}
                done_args = event.get("arguments")
                if isinstance(done_args, str) and done_args:
                    accumulated_tool_calls[item_id]["arguments"] = done_args
                done_name = event.get("name")
                if isinstance(done_name, str) and done_name:
                    accumulated_tool_calls[item_id]["name"] = done_name
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, dict):
                    if item.get("type") == "function_call":
                        item_id = item.get("id", "")
                        call_id = item.get("call_id", "")
                        name = item.get("name", "")
                        if item_id not in accumulated_tool_calls:
                            accumulated_tool_calls[item_id] = {"name": name, "call_id": call_id, "arguments": ""}
                        else:
                            if call_id:
                                accumulated_tool_calls[item_id]["call_id"] = call_id
                            if name:
                                accumulated_tool_calls[item_id]["name"] = name
                    elif item.get("type") == "mcp_call":
                        accumulated_output.append(item)
                        item_id = item.get("id", "")
                        tool_name = item.get("name") or item.get("server_label") or "mcp_call"
                        arguments = item.get("arguments") or {}
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments)
                        if item_id and item_id not in emitted_tool_call_ids:
                            emitted_tool_call_ids.add(item_id)
                            yield {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": item_id,
                                        "call_id": item_id,
                                        "type": "function",
                                        "function": {"name": tool_name, "arguments": arguments},
                                    }
                                ],
                                "usage": None,
                                "finish_reason": None,
                                "reasoning": None,
                                "stream_event_only": True,
                            }
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    if item.get("type") == "function_call":
                        item_id = item.get("id", "")
                        call_id = item.get("call_id", "")
                        name = item.get("name", "")
                        item_arguments = item.get("arguments", "")
                        if item_id not in accumulated_tool_calls:
                            accumulated_tool_calls[item_id] = {"name": name, "call_id": call_id, "arguments": item_arguments}
                        else:
                            accumulated_tool_calls[item_id]["name"] = name
                            accumulated_tool_calls[item_id]["call_id"] = call_id
                    elif item.get("type") == "mcp_call":
                        item_id = item.get("id")
                        for index, existing in enumerate(accumulated_output):
                            if isinstance(existing, dict) and existing.get("id") == item_id:
                                accumulated_output[index] = item
                                break
                        else:
                            accumulated_output.append(item)
                        tool_name = item.get("name") or item.get("server_label") or "mcp_call"
                        arguments = item.get("arguments") or {}
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments)
                        if item_id:
                            if item_id not in emitted_tool_call_ids:
                                emitted_tool_call_ids.add(item_id)
                                yield {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": item_id,
                                            "call_id": item_id,
                                            "type": "function",
                                            "function": {"name": tool_name, "arguments": arguments},
                                        }
                                    ],
                                    "usage": None,
                                    "finish_reason": None,
                                    "reasoning": None,
                                    "stream_event_only": True,
                                }
                        output = item.get("output")
                        if output is not None:
                            yield {
                                "content": None,
                                "tool_calls": None,
                                "tool_results": [
                                    {
                                        "name": tool_name,
                                        "content": output if isinstance(output, str) else json.dumps(output),
                                        "call_id": item_id or "",
                                    }
                                ],
                                "usage": None,
                                "finish_reason": None,
                                "reasoning": None,
                                "stream_event_only": True,
                            }
                    else:
                        accumulated_output.append(item)

        tool_calls_list = [
            {"id": k, "call_id": v["call_id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for k, v in accumulated_tool_calls.items()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": finish_reason, "reasoning": None, "output": accumulated_output, "service_tier": service_tier, "incomplete_details": incomplete_details}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def openai_test_model(configuration, api_key):
    headers = _openai_headers(api_key)
    try:
        response_data, _ = await fetch(url=OPENAI_CHAT_COMPLETIONS_URL, method="POST", headers=headers, json_body=configuration)
        return {"success": True, "response": response_data}
    except Exception as error:
        return {"success": False, "error": str(error), "status_code": getattr(error, "status_code", None)}


async def openai_response_model(
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
        headers = _openai_headers(apiKey)

        async def api_call_with_retry(config, max_retries=2):
            current_config = copy.deepcopy(config)

            for attempt in range(max_retries + 1):
                try:
                    response_data, _ = await fetch(url=OPENAI_RESPONSES_URL, method="POST", headers=headers, json_body=current_config)
                    return {"success": True, "response": response_data}
                except Exception as error:
                    error_str = str(error)
                    if "Duplicate item found with id" in error_str and attempt < max_retries:
                        logger.warning(f"Duplicate ID error detected on attempt {attempt + 1}: {error_str}")
                        logger.info("Attempting to fix duplicate IDs and retry...")
                        current_config = remove_duplicate_ids_from_input(current_config)
                        execution_time_logs.append(
                            {"step": f"{service} Retry attempt {attempt + 1} - Fixed duplicate IDs", "time_taken": 0}
                        )
                        continue
                    traceback.print_exc()
                    return {
                        "success": False,
                        "error": error_str,
                        "status_code": getattr(error, "status_code", None),
                    }

            return {"success": False, "error": "Max retries exceeded", "status_code": None}

        async def api_call(config):
            return await api_call_with_retry(config)

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
