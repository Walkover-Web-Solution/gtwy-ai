import copy
import json
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


async def _handle_streaming_response(client, config):
    """
    Handle streaming response from OpenAI Responses API.
    Accumulates streamed events into a complete response dict matching the non-streaming format.
    Similar pattern to the Anthropic streaming implementation in anthropicModelRun.py.
    """
    accumulated_response = {
        "id": "",
        "object": "response",
        "created_at": 0,
        "status": "",
        "model": config.get("model", ""),
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }

    # Track output items by index for incremental assembly
    output_items = {}
    # Track content parts within output items: {output_index: {part_index: part_data}}
    content_parts = {}
    # Track function call argument deltas per output item
    function_call_args = {}

    stream = await client.responses.create(**config, stream=True)

    async for event in stream:
        event_type = event.type

        if event_type == "response.created":
            if hasattr(event, "response") and event.response:
                resp = event.response
                accumulated_response["id"] = getattr(resp, "id", "")
                accumulated_response["created_at"] = getattr(resp, "created_at", 0)
                accumulated_response["model"] = getattr(resp, "model", config.get("model", ""))
                accumulated_response["status"] = getattr(resp, "status", "")

        elif event_type == "response.output_item.added":
            index = getattr(event, "output_index", len(output_items))
            item = event.item
            item_dict = item.to_dict() if hasattr(item, "to_dict") else {"type": getattr(item, "type", "unknown")}
            output_items[index] = item_dict
            content_parts[index] = {}

        elif event_type == "response.content_part.added":
            output_index = getattr(event, "output_index", 0)
            part_index = getattr(event, "content_index", len(content_parts.get(output_index, {})))
            part = event.part
            part_dict = part.to_dict() if hasattr(part, "to_dict") else {"type": getattr(part, "type", "unknown")}
            if output_index not in content_parts:
                content_parts[output_index] = {}
            content_parts[output_index][part_index] = part_dict

        elif event_type == "response.output_text.delta":
            output_index = getattr(event, "output_index", 0)
            part_index = getattr(event, "content_index", 0)
            delta = getattr(event, "delta", "")
            if output_index in content_parts and part_index in content_parts[output_index]:
                content_parts[output_index][part_index].setdefault("text", "")
                content_parts[output_index][part_index]["text"] += delta

        elif event_type == "response.function_call_arguments.delta":
            output_index = getattr(event, "output_index", 0)
            delta = getattr(event, "delta", "")
            function_call_args.setdefault(output_index, "")
            function_call_args[output_index] += delta

        elif event_type == "response.output_item.done":
            index = getattr(event, "output_index", 0)
            item = event.item
            item_dict = item.to_dict() if hasattr(item, "to_dict") else output_items.get(index, {})
            output_items[index] = item_dict

        elif event_type == "response.completed":
            if hasattr(event, "response") and event.response:
                resp = event.response
                # Use the final complete response directly
                final_dict = resp.to_dict() if hasattr(resp, "to_dict") else None
                if final_dict:
                    return final_dict
                # Fallback: update accumulated response with final metadata
                accumulated_response["status"] = getattr(resp, "status", "completed")
                if hasattr(resp, "usage") and resp.usage:
                    usage = resp.usage
                    accumulated_response["usage"] = {
                        "input_tokens": getattr(usage, "input_tokens", 0),
                        "output_tokens": getattr(usage, "output_tokens", 0),
                        "total_tokens": getattr(usage, "total_tokens", 0),
                    }

    # Assemble final output from tracked items (fallback if response.completed didn't provide full response)
    final_output = []
    for idx in sorted(output_items.keys()):
        item = output_items[idx]
        # For message-type items, assemble content parts
        if item.get("type") == "message" and idx in content_parts:
            assembled_content = [content_parts[idx][p_idx] for p_idx in sorted(content_parts[idx].keys())]
            item["content"] = assembled_content
        # For function_call items, parse accumulated arguments
        if item.get("type") == "function_call" and idx in function_call_args:
            item["arguments"] = function_call_args[idx]
        final_output.append(item)

    accumulated_response["output"] = final_output 
    return accumulated_response


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
            is_streaming = current_config.pop("stream", False)

            for attempt in range(max_retries + 1):
                try:
                    if is_streaming:
                        response = await _handle_streaming_response(client, current_config)
                    else:
                        response = await client.responses.create(**current_config)
                        response = response.to_dict()
                    return {"success": True, "response": response}
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


def _sse_event(event, data):
    return {"event": event, "data": json.dumps(data)}


async def openai_response_model_stream(
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
    """
    Async generator that streams SSE events from OpenAI Responses API.
    Yields SSE event dicts for text deltas, then yields a final 'done' event with the complete response.
    """
    client = AsyncOpenAI(api_key=apiKey)
    config = copy.deepcopy(configuration)
    config.pop("stream", None)

    timer.start()

    try:
        stream = await client.responses.create(**config, stream=True)
        final_response = None

        async for event in stream:
            event_type = event.type

            if event_type == "response.output_text.delta":
                yield _sse_event("delta", {"chunk": getattr(event, "delta", "")})

            elif event_type == "response.reasoning_summary_text.delta":
                yield _sse_event("thinking", {"chunk": getattr(event, "delta", "")})

            elif event_type == "response.function_call_arguments.delta":
                yield _sse_event("function_call_delta", {
                    "delta": getattr(event, "delta", ""),
                    "output_index": getattr(event, "output_index", 0),
                })

            elif event_type == "response.output_item.added":
                item = event.item
                item_dict = item.to_dict() if hasattr(item, "to_dict") else {}
                yield _sse_event("output_item_added", item_dict)

            elif event_type == "response.completed":
                if hasattr(event, "response") and event.response:
                    final_response = event.response.to_dict() if hasattr(event.response, "to_dict") else None

        execution_time_logs.append({
            "step": f"{service} Processing time for call :- {count + 1}",
            "time_taken": timer.stop("API chat completion"),
        })

        if final_response:
            token_calculator.calculate_usage(final_response)
            yield _sse_event("response.completed", {"success": True, "response": final_response})
        else:
            yield _sse_event("error", {"success": False, "error": "Stream completed without final response"})

    except Exception as error:
        execution_time_logs.append({
            "step": f"{service} Processing time for call :- {count + 1}",
            "time_taken": timer.stop("API chat completion"),
        })
        logger.error(f"Streaming error: {str(error)}, {traceback.format_exc()}")
        yield _sse_event("error", {"success": False, "error": str(error), "status_code": getattr(error, "status_code", None)})


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
