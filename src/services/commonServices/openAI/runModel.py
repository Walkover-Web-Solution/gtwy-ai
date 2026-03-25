import copy
import traceback

from openai import AsyncOpenAI

# from src.services.utils.unified_token_validator import validate_openai_token_limit
from globals import logger
from src.exceptions import ApiCallError

from ..api_executor import execute_api_call


def remove_duplicate_ids_from_input(configuration):
    """
    Remove duplicate items with same IDs from the input array to prevent OpenAI API errors
    """
    config_copy = copy.deepcopy(configuration)

    if "input" not in config_copy:
        return config_copy

    input_array = config_copy["input"]
    seen_ids = set()

    # Filter out duplicate items instead of creating new IDs
    filtered_input = []

    for item in input_array:
        if isinstance(item, dict) and "id" in item:
            original_id = item["id"]
            # If ID is duplicate, skip this item (remove it)
            if original_id in seen_ids:
                logger.info(f"Removing duplicate item with ID: {original_id}")
                continue  # Skip this duplicate item
            else:
                seen_ids.add(original_id)
                filtered_input.append(item)
        else:
            # Items without ID are always included
            filtered_input.append(item)

    # Update the configuration with filtered input
    config_copy["input"] = filtered_input

    return config_copy


async def openai_response_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for openai responses API."""
    client = AsyncOpenAI(api_key=apiKey)
    config = {**configuration}
    accumulated_output = []
    accumulated_tool_calls = {}  # item_id -> {"name": str, "call_id": str, "arguments": str}
    usage = {}
    finish_reason = None
    try:
        stream = await client.responses.create(**config)
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                yield {"content": event.delta, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            elif event_type == "response.reasoning_summary_text.delta":
                yield {"content": None, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": event.delta}
            elif event_type == "response.function_call_arguments.delta":
                item_id = getattr(event, "item_id", "")
                if item_id not in accumulated_tool_calls:
                    accumulated_tool_calls[item_id] = {"name": "", "call_id": "", "arguments": ""}
                accumulated_tool_calls[item_id]["arguments"] += getattr(event, "delta", "")
            elif event_type == "response.function_call_arguments.done":
                item_id = getattr(event, "item_id", "")
                if item_id not in accumulated_tool_calls:
                    accumulated_tool_calls[item_id] = {"name": "", "call_id": "", "arguments": ""}
                done_args = getattr(event, "arguments", None)
                if isinstance(done_args, str) and done_args:
                    accumulated_tool_calls[item_id]["arguments"] = done_args
                done_name = getattr(event, "name", None)
                if isinstance(done_name, str) and done_name:
                    accumulated_tool_calls[item_id]["name"] = done_name
            elif event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if isinstance(item, dict):
                    item_type = item.get("type")
                else:
                    item_type = getattr(item, "type", None) if item else None
                if item_type == "function_call":
                    if isinstance(item, dict):
                        item_id = item.get("id", "")
                        call_id = item.get("call_id", "")
                        name = item.get("name", "")
                    else:
                        item_id = getattr(item, "id", "")
                        call_id = getattr(item, "call_id", "")
                        name = getattr(item, "name", "")
                    if item_id not in accumulated_tool_calls:
                        accumulated_tool_calls[item_id] = {"name": name, "call_id": call_id, "arguments": ""}
                    else:
                        if call_id:
                            accumulated_tool_calls[item_id]["call_id"] = call_id
                        if name:
                            accumulated_tool_calls[item_id]["name"] = name
            elif event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if isinstance(item, dict):
                    item_type = item.get("type")
                else:
                    item_type = getattr(item, "type", None) if item else None
                if item_type == "function_call":
                    if isinstance(item, dict):
                        item_id = item.get("id", "")
                        call_id = item.get("call_id", "")
                        name = item.get("name", "")
                        item_arguments = item.get("arguments", "")
                    else:
                        item_id = getattr(item, "id", "")
                        call_id = getattr(item, "call_id", "")
                        name = getattr(item, "name", "")
                        item_arguments = getattr(item, "arguments", "")
                    if item_id not in accumulated_tool_calls:
                        accumulated_tool_calls[item_id] = {"name": name, "call_id": call_id, "arguments": item_arguments}
                    else:
                        accumulated_tool_calls[item_id]["name"] = name
                        accumulated_tool_calls[item_id]["call_id"] = call_id
                else:
                    if item is not None:
                        accumulated_output.append(item.model_dump() if hasattr(item, "model_dump") else vars(item))
            elif event_type == "response.completed":
                resp = getattr(event, "response", None)
                if resp:
                    usage_obj = getattr(resp, "usage", None)
                    if usage_obj:
                        usage = usage_obj.model_dump() if hasattr(usage_obj, "model_dump") else vars(usage_obj)
                    finish_reason = getattr(resp, "status", None)
        tool_calls_list = [
            {"id": k, "call_id": v["call_id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for k, v in accumulated_tool_calls.items()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": finish_reason, "reasoning": None, "output": accumulated_output}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def openai_test_model(configuration, api_key):
    openAI = AsyncOpenAI(api_key=api_key)
    try:
        chat_completion = await openAI.chat.completions.create(**configuration)
        return {"success": True, "response": chat_completion.to_dict()}
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
):
    try:
        # # Validate token count before making API call (raises exception if invalid)
        # model_name = configuration.get('model')
        # validate_openai_token_limit(configuration, model_name, 'openai_response')

        client = AsyncOpenAI(api_key=apiKey)

        # Define the API call function with retry mechanism for duplicate ID errors
        async def api_call_with_retry(config, max_retries=2):
            current_config = copy.deepcopy(config)

            for attempt in range(max_retries + 1):
                try:
                    responses = await client.responses.create(**current_config)
                    return {"success": True, "response": responses.to_dict()}
                except Exception as error:
                    error_str = str(error)

                    # Check if it's a duplicate item error
                    if "Duplicate item found with id" in error_str and attempt < max_retries:
                        logger.warning(f"Duplicate ID error detected on attempt {attempt + 1}: {error_str}")
                        logger.info("Attempting to fix duplicate IDs and retry...")

                        # Remove duplicate IDs and regenerate unique ones
                        current_config = remove_duplicate_ids_from_input(current_config)

                        # Log the retry attempt
                        execution_time_logs.append(
                            {"step": f"{service} Retry attempt {attempt + 1} - Fixed duplicate IDs", "time_taken": 0}
                        )

                        continue  # Retry with fixed configuration
                    else:
                        # For non-duplicate errors or max retries reached, return the error
                        traceback.print_exc()
                        return {
                            "success": False,
                            "error": error_str,
                            "status_code": getattr(error, "status_code", None),
                        }

            # This should never be reached, but just in case
            return {"success": False, "error": "Max retries exceeded", "status_code": None}

        # Define the API call function for execute_api_call
        async def api_call(config):
            return await api_call_with_retry(config)

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


async def openai_completion(
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
        openAI = AsyncOpenAI(api_key=apiKey)

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
