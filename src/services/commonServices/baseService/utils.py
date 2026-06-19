import asyncio
import datetime
import json
import re
from typing import Any

import httpx
from fastapi import Request
from google.genai import types

from globals import *
from globals import logger, traceback
from src.configs.constant import GPT_MEMORY_TURNS_PER_CYCLE, inbuild_tools, redis_keys, service_name
from src.configs.service_registry import has_openai_choices_shape, uses_string_tool_choice
from src.controllers.rag_controller import get_text_from_vectorsQuery
from src.services.utils.mcp_utils import MCP_NAME_SUFFIX, display_mcp_tool_name
from src.services.cache_service import REDIS_PREFIX, client, find_in_cache, incr_in_cache, store_in_cache
from src.services.mcp_gateway.client import call_mcp_tool
from src.services.utils.ai_call_util import call_gtwy_agent
from src.services.utils.apiservice import fetch
from src.services.utils.built_in_tools.firecrawl import call_firecrawl_scrape


def clean_json(data):
    """Recursively remove keys with empty string, empty list, or empty dictionary."""
    if isinstance(data, dict):
        return {k: clean_json(v) for k, v in data.items() if v not in [{}, "", []]}
    elif isinstance(data, list):
        return [clean_json(item) for item in data]
    else:
        return data


def get_nested_value(dictionary, key_path):
    keys = key_path.split(".")
    for key in keys:
        if isinstance(dictionary, dict) and key in dictionary:
            dictionary = dictionary[key]
        else:
            return None
    return dictionary


def apply_variable_path_filters(
    properties, variables=None, variables_path=None, function_name=None, parent_key=None, parentValue=None
):
    if variables is None:
        variables = {}
    if variables_path is None:
        variables_path = {}
    if not isinstance(properties, dict):
        return properties

    transformed_properties = {}
    function_variables_path = (variables_path or {}).get(function_name, {})

    for key, value in properties.items():
        if not isinstance(value, dict):
            transformed_properties[key] = value
            continue

        transformed_value = value.copy()

        key_to_find = f"{parent_key}.{key}" if parent_key else key
        if key_to_find in function_variables_path:
            variable_path_value = function_variables_path[key_to_find]
            if_variable_has_value = get_nested_value(variables, variable_path_value)
            if if_variable_has_value is not None:
                if parentValue and "required" in parentValue and key in parentValue["required"]:
                    parentValue["required"].remove(key)
                continue

        if isinstance(transformed_value.get("properties"), dict):
            transformed_value["properties"] = apply_variable_path_filters(
                transformed_value["properties"],
                variables,
                variables_path,
                function_name,
                key,
                transformed_value,
            )

        items = transformed_value.get("items")
        if isinstance(items, dict):
            transformed_items = items.copy()
            if isinstance(transformed_items.get("properties"), dict):
                transformed_items["properties"] = apply_variable_path_filters(
                    transformed_items["properties"],
                    variables,
                    variables_path,
                    function_name,
                    key,
                    transformed_items,
                )
            if isinstance(transformed_items.get("items"), dict):
                transformed_items["items"] = apply_variable_path_filters(
                    transformed_items["items"],
                    variables,
                    variables_path,
                    function_name,
                    key,
                    transformed_items,
                )
            transformed_value["items"] = transformed_items

        transformed_properties[key] = transformed_value

    return transformed_properties


def validate_tool_call(service, response):
    match service: # TODO: Fix validation process.
        case s if has_openai_choices_shape(s):  # openai_chat wire format (choices[0].message)
            tool_calls = response.get('choices', [])[0].get('message', {}).get("tool_calls", [])
            return len(tool_calls) > 0 if tool_calls is not None else False
        case "openai":
            return any(output.get("type") == "function_call" for output in response.get("output", []))
        case "anthropic":
            return response.get('stop_reason') == 'tool_use'
        case 'gemini':
            candidates = response.get('candidates', [])
            if not candidates:
                return False
            parts = candidates[0].get('content', {}).get('parts', [])
            return any(isinstance(part, dict) and part.get('function_call') is not None for part in parts)
        case _:
            return False


def resolve_url_params(url, method, data, query_param_keys=None):
    """
    Replace :param or {param} route params in URL with values from data dict.
    For GET requests all remaining args become query params.
    For non-GET, keys listed in query_param_keys are sent as query params; the rest go as JSON body.
    Returns (resolved_url, query_params_dict_or_None, body_or_None).
    """
    if not data:
        return url, None, None

    data = dict(data)
    query_param_keys = query_param_keys or []

    def _replace(match):
        key = match.group(1) or match.group(2)
        if key in data:
            return str(data.pop(key))
        return match.group(0)

    resolved_url = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)|{([A-Za-z_][A-Za-z0-9_]*)}", _replace, url)

    def _coerce(v):
        if isinstance(v, bool):
            return str(v).lower()
        if not isinstance(v, (str, int, float)):
            return str(v)
        return v

    if method.upper() == "GET":
        coerced = {k: _coerce(v) for k, v in data.items()}
        return resolved_url, coerced or None, None

    query_params = {k: _coerce(data.pop(k)) for k in list(query_param_keys) if k in data}
    return resolved_url, query_params or None, data or None


async def axios_work(data, function_payload):
    try:
        method = function_payload.get("method", "POST")
        resolved_url, query_params, body = resolve_url_params(
            function_payload.get("url"),
            method,
            data,
            function_payload.get("query_params", []),
        )
        response, rs_headers = await fetch(
            resolved_url, method, function_payload.get("headers", {}), query_params, body
        )  # required is not send then it will still hit the curl
        return {
            "response": response,
            "metadata": {"flowHitId": rs_headers.get("flowHitId"), "type": "function"},
            "status": 1,
        }

    except Exception as err:
        logger.error("Error calling function axios_work => ", function_payload.get("url"), err)
        return {"response": str(err), "metadata": {"type": "function"}, "status": 0}


def disable_tool_call(configuration: dict, service: str):
    if uses_string_tool_choice(service):
        configuration["tool_choice"] = "none"

    elif service == service_name["gemini"]:
        # Disabling Tool Call
        configuration["config"].tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="NONE"
            )
        )
        # Disabling Auto Tool call (Required)
        configuration["config"].automatic_function_calling = types.AutomaticFunctionCallingConfig(
            disable=True
        )

    elif service == service_name["anthropic"]:
        configuration["tool_choice"] = {"type": "none"}

def tool_call_formatter(configuration: dict, service: str, variables: dict, variables_path: dict) -> dict:  # changes
    if has_openai_choices_shape(service):
        # All openai_chat services share the function-nested tool schema
        # (openai_completion, open_router, mistral, neev_cloud, moonshot,
        # deepseek, groq, grok). openai (flat) / gemini / anthropic differ below.
        data_to_send = [
            {
                "type": "function",
                "function": {
                    "name": transformed_tool["name"],
                    # "strict": True,
                    "description": transformed_tool["description"],
                    "parameters": {
                        "type": "object",
                        "properties": clean_json(
                            apply_variable_path_filters(
                                transformed_tool.get("properties", {}),
                                variables=variables,
                                variables_path=variables_path,
                                function_name=transformed_tool["name"],
                                parentValue={"required": transformed_tool.get("required", [])},
                            )
                        ),
                        "required": transformed_tool.get("required"),
                        # "additionalProperties": False,
                    },
                },
            }
            for transformed_tool in configuration.get("tools", [])
        ]
        return data_to_send

    elif service == service_name['gemini']:
        gemini_tools = []
        function_declarations = [
            {
                "name": transformed_tool['name'],
                "description": transformed_tool['description'],
                "parameters": {
                    "type": "object",
                    "properties": clean_json(
                        apply_variable_path_filters(
                            transformed_tool.get("properties", {}),
                            variables=variables,
                            variables_path=variables_path,
                            function_name=transformed_tool["name"],
                            parentValue={"required": transformed_tool.get("required", [])},
                        )
                    ),
                    "required": transformed_tool.get('required'),
                }
            } for transformed_tool in configuration.get('tools', [])
        ]
        if function_declarations:
            from google.genai import types
            gemini_tools.append(types.Tool(function_declarations=function_declarations))

        return gemini_tools
    elif service == service_name['openai']:
        data_to_send =  [
            {
                "type": "function",
                "name": transformed_tool["name"],
                # "strict": True,
                "description": transformed_tool["description"],
                "parameters": {
                    "type": "object",
                    "properties": clean_json(
                        apply_variable_path_filters(
                            transformed_tool.get("properties", {}),
                            variables=variables,
                            variables_path=variables_path,
                            function_name=transformed_tool["name"],
                            parentValue={"required": transformed_tool.get("required", [])},
                        )
                    ),
                    "required": transformed_tool.get("required"),
                    # "additionalProperties": False,
                },
            }
            for transformed_tool in configuration.get("tools", [])
        ]
        return data_to_send
    elif service == service_name["anthropic"]:
        return [
            transformed_tool
            if transformed_tool["name"] == "JSON_Schema_Response_Format"
            else {
                "name": transformed_tool["name"],
                "description": transformed_tool["description"],
                "input_schema": {
                    "type": "object",
                    "properties": clean_json(
                        apply_variable_path_filters(
                            transformed_tool.get("properties", {}),
                            variables=variables,
                            variables_path=variables_path,
                            function_name=transformed_tool["name"],
                            parentValue={"required": transformed_tool.get("required", [])},
                        )
                    ),
                    "required": transformed_tool.get("required"),
                },
            }
            for transformed_tool in configuration.get("tools", [])
        ]

def reasoning_formatter(service: str, new_config: dict) -> None:
    if service == service_name["openai"]:
        if isinstance(new_config.get("reasoning"), dict):
            new_config["reasoning"]["summary"] = "auto"

    elif service == service_name["gemini"]:
        effort = new_config["reasoning"].get("effort", "medium")
        new_config["thinking_config"] = types.ThinkingConfig(
            include_thoughts=True,
            thinking_level=effort
        )
        new_config.pop("reasoning", None)

    elif service == service_name["anthropic"]:
        new_config["thinking"] = {"type": "adaptive"}
        effort = new_config["reasoning"].get("effort", "medium")
        if new_config.get("output_config"):
            new_config["output_config"]["effort"] = effort
        else:
            new_config["output_config"] = {"effort": effort}

        new_config.pop("reasoning", None)

    elif service == service_name["groq"]:
        effort = new_config["reasoning"].get("effort", "medium")
        new_config["reasoning_effort"] = effort
        new_config.pop("reasoning", None)

    elif service == service_name["deepseek"]:
        effort = new_config["reasoning"].get("effort", "medium")
        new_config["reasoning_effort"] = effort
        new_config["extra_body"] = {"thinking": {"type": "enabled"}}
        new_config.pop("reasoning", None)

    # Grok, OpenRouter, Mistral, AI-ML do not support Reasoning from our side


async def send_request(url, data, method, headers):
    try:
        if isinstance(headers, str):
            headers = json.loads(headers)
        return await fetch(url, method, headers, None, data)
    except Exception as e:
        logger.error(f"Unexpected error:, {url}, {str(e)}")
        return {"error": "Unexpected error", "details": str(e)}


async def send_message(cred, data):
    try:
        response = await fetch(
            f"https://api.rtlayer.com/message?apiKey={cred['apikey']}",
            "POST",
            None,
            None,
            {**cred, "message": json.dumps(data)},
        )
        return response
    except httpx.RequestError as error:
        logger.error(f"send message error=>, {str(error)}")
    except Exception as e:
        logger.error(f"Unexpected send message error=>, {str(e)}")


async def sendResponse(response_format, data, success=False, variables=None, meta=None):
    if variables is None:
        variables = {}
    data_to_send = {"response" if success else "error": data, "success": success}
    match response_format["type"]:
        case "RTLayer":
            return await send_message(cred=response_format["cred"], data=data_to_send)
        case "webhook":
            data_to_send["variables"] = variables
            if meta:
                data_to_send["meta"] = meta
            return await send_request(**response_format["cred"], method="POST", data=data_to_send)


async def process_data_and_run_tools(codes_mapping, self):
    try:
        self.timer.start()
        executed_functions = []
        responses = []
        tool_call_logs = {**codes_mapping}

        # Prepare tasks for async execution
        tasks = []
        for tool_call_key, tool in codes_mapping.items():
            original_name = tool["name"]
            name = original_name

            # Gemini might return a tool_call with prefix
            if name not in self.tool_id_and_name_mapping and isinstance(name, str) and "." in name:
                short_name = name.rsplit(".", 1)[-1]
                if short_name in self.tool_id_and_name_mapping:
                    name = short_name

            tool_mapping = self.tool_id_and_name_mapping.get(name) or {"error": True, "response": "Wrong Function name"}
            display_tool_name = tool_mapping.get("mcp_tool") if tool_mapping.get("type") == "MCP" else display_mcp_tool_name(name)
            tool_data = {**tool, **tool_mapping, "name": name, "model_tool_name": original_name, "display_tool_name": display_tool_name}

            if not tool_data.get("response"):
                # if function is present in db/NO response, create task for async processing
                if self.tool_id_and_name_mapping[name].get("type") == "RAG":
                    # Get the resource_to_collection_mapping from tool_id_and_name_mapping
                    resource_to_collection_mapping = self.tool_id_and_name_mapping[name].get(
                        "resource_to_collection_mapping", {}
                    )
                    task = get_text_from_vectorsQuery(
                        {**tool_data.get("args"), "org_id": self.org_id},
                        Flag=True,
                        owner_id=self.owner_id,
                        resource_to_collection_mapping=resource_to_collection_mapping,
                    )
                elif self.tool_id_and_name_mapping[name].get("type") == "AGENT":
                    agent_args = {
                        "org_id": self.org_id,
                        "bridge_id": self.tool_id_and_name_mapping[name].get("bridge_id"),
                        "user": tool_data.get("args").get("_query"),
                        "variables": {key: value for key, value in tool_data.get("args").items() if key != "user"},
                        "message_id": self.message_id
                    }

                    if self.stream_mode and self.streamer:
                        agent_args["injected_streamer"] = self.streamer
                        agent_args["nested_stream_call"] = True

                    # Add thread_id and sub_thread_id if bridge requires it
                    if self.tool_id_and_name_mapping[name].get("requires_thread_id", False):
                        agent_args["thread_id"] = self.thread_id
                        agent_args["sub_thread_id"] = self.sub_thread_id
                    if self.tool_id_and_name_mapping[name].get("version_id", False):
                        agent_args["version_id"] = self.tool_id_and_name_mapping[name].get("version_id")

                    # Pass timer state to maintain latency tracking in recursive calls
                    if hasattr(self, "timer") and hasattr(self.timer, "getTime"):
                        agent_args["timer_state"] = self.timer.getTime()

                    # Pass bridge_configurations if available
                    if hasattr(self, "bridge_configurations") and self.bridge_configurations:
                        agent_args["bridge_configurations"] = self.bridge_configurations


                    task = call_gtwy_agent(agent_args)
                elif self.tool_id_and_name_mapping[name].get("type") == inbuild_tools["Gtwy_Web_Search"]:
                    task = call_firecrawl_scrape(tool_data.get("args"))
                elif self.tool_id_and_name_mapping[name].get("type") == "MCP":
                    task = call_mcp_tool(tool_data.get("args"), self.tool_id_and_name_mapping[name])
                else:
                    task = axios_work(tool_data.get("args"), self.tool_id_and_name_mapping[name])
                tasks.append((tool_call_key, tool_data, task))
                executed_functions.append(name)
            else:
                # If function is not present in db/response exists, append to responses
                responses.append(
                    {
                        "tool_call_id": tool_call_key,
                        "role": "tool",
                        "name": tool["name"],
                        "content": json.dumps(tool_data["response"]),
                    }
                )
                # Update tool_call_logs with existing response
                tool_call_logs[tool_call_key] = {**tool, "name": tool_data.get("display_tool_name"), "response": tool_data["response"]}

        # Execute all tasks concurrently if any exist
        if tasks:
            task_results = await asyncio.gather(
                *[task[2] for task in tasks], return_exceptions=True
            )  # return_exceptions use for the handle the error occurs from the task

            # Process each result
            for i, (tool_call_key, tool_data, _) in enumerate(tasks):
                result = task_results[i]

                # Handle any exceptions or errors
                if isinstance(result, Exception):
                    response = {"error": "Error during async task execution", "details": str(result)}
                elif tool_data.get("error"):
                    response = {"error": "Args / Input is not proper JSON"}
                else:
                    response = (
                        result.get("response", "")
                        if result.get("status") == 1
                        else {"error": result.get("response", "")}
                    )

                # Append formatted response
                responses.append(
                    {
                        "tool_call_id": tool_call_key,  # Replacing with tool_call_key
                        "role": "tool",
                        "name": tool_data["name"],
                        "content": json.dumps(response),
                    }
                )

                # Update tool_call_logs with the response
                tool_call_logs[tool_call_key] = {
                    **tool_data,
                    "name": tool_data.get("display_tool_name"),
                    "data": result or response,
                    "id": self.tool_id_and_name_mapping[tool_data["name"]].get("mcp_tool") or self.tool_id_and_name_mapping[tool_data["name"]].get("name"),
                }
        # Create mapping by tool_call_id (now tool_call_key) for return
        mapping = {resp["tool_call_id"]: resp for resp in responses}

        # Record executed function names and timing
        executed_names = ", ".join(executed_functions) if executed_functions else "No functions executed"
        self.function_time_logs.append(
            {"step": executed_names, "time_taken": self.timer.stop("process_data_and_run_tools")}
        )

        return responses, mapping, tool_call_logs

    except Exception as error:
        print(f"Error in process_data_and_run_tools: {error}")
        traceback.print_exc()
        raise error


def make_code_mapping_by_service(responses, service):
    codes_mapping = {}
    function_list = []
    match service:
        case s if has_openai_choices_shape(s):  # openai_chat wire format (choices[0].message)

            for tool_call in responses['choices'][0]['message']['tool_calls']:
                name = tool_call['function']['name']
                error = False
                try:
                    args = json.loads(tool_call["function"]["arguments"]) if isinstance(tool_call["function"]["arguments"], str) else (tool_call["function"]["arguments"] or {})
                except (json.JSONDecodeError, TypeError):
                    args = {"error": str(tool_call["function"]["arguments"])}
                    error = True
                codes_mapping[tool_call["id"]] = {"name": name, "args": args, "error": error}
                function_list.append(name)

        case 'gemini':
            for part in responses['candidates'][0]["content"]["parts"]:
                function_call = part.get('function_call') if isinstance(part, dict) else None
                if function_call:
                    name = function_call.get("name")
                    args = function_call.get('args')

                    codes_mapping[function_call.get('id')] = {
                        'name': name,
                        'args': args,
                        "error": False
                    }
                    function_list.append(name)

        case 'openai':

            for tool_call in [output for output in responses['output'] if output.get('type') == 'function_call']:
                name = tool_call['name']
                error = False
                try:
                    args = json.loads(tool_call["arguments"]) if isinstance(tool_call["arguments"], str) else (tool_call["arguments"] or {})
                except (json.JSONDecodeError, TypeError):
                    args = {"error": str(tool_call["arguments"])}
                    error = True
                codes_mapping[tool_call["id"]] = {"name": name, "args": args, "error": error}
                function_list.append(name)
        case "anthropic":
            for tool_call in [
                item for item in responses["content"] if item["type"] == "tool_use"
            ]:  # Skip the first item
                name = tool_call["name"]
                args = tool_call["input"]
                codes_mapping[tool_call["id"]] = {"name": name, "args": args, "error": False}
                function_list.append(name)
        case _:
            return {}, []
    return codes_mapping, function_list


def convert_datetime(obj):
    """Recursively convert datetime objects in a dictionary or list to ISO format strings."""
    if isinstance(obj, dict):
        return {k: convert_datetime(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime(item) for item in obj]
    elif isinstance(obj, datetime.datetime):
        return obj.isoformat()  # Convert datetime to ISO format string
    else:
        return obj


async def make_request_data(request: Request):
    body = await request.json()
    state_data = {}
    path_params = {}

    attributes = ["version", "profile"]
    for attr in attributes:
        if hasattr(request.state, attr):
            state_data[attr] = getattr(request.state, attr)

    if hasattr(request.state, "timer"):
        state_data["timer"] = request.state.timer

    if hasattr(request, "path_params"):
        path_params = request.path_params

    body = convert_datetime(body)
    state_data = convert_datetime(state_data)
    id_to_use = body.get('version') if body.get('version') else (body.get('agent_id') or body.get('bridge_id'))

    result = {"body": body, "state": state_data, "path_params": path_params, "id_to_use": id_to_use}
    return result


async def make_request_data_and_publish_sub_queue(parsed_data, result, params, thread_info=None):
    suggestion_content = {"data": {"content": {}}}
    suggestion_content["data"]["content"] = result.get("historyParams", {}).get("message")

    # Extract user and assistant messages for Hippocampus
    user_message = parsed_data.get("user", "")
    assistant_message = result.get("historyParams", {}).get("message", "")

    gpt_memory_enabled = bool(parsed_data.get("gpt_memory"))
    should_fire_gpt_memory = False
    pending_turns: list = []
    if gpt_memory_enabled and parsed_data.get("id"):
        counter_key = f"{redis_keys['gpt_memory_counter_']}{parsed_data['id']}"
        count = await incr_in_cache(counter_key)
        should_fire_gpt_memory = count > 0 and count % GPT_MEMORY_TURNS_PER_CYCLE == 0
        if should_fire_gpt_memory:
            prior_conversation = (
                parsed_data.get("configuration", {}).get("conversation") or []
            )
            pending_turns = [
                msg
                for msg in prior_conversation
                if msg.get("role") not in ("tool", "tools_call")
            ]
            if user_message:
                pending_turns.append({"role": "user", "content": user_message})
            pending_turns.append({"role": "assistant", "content": assistant_message})

    data = {
        "metrics_service": {
            "dataset": [parsed_data.get("usage", {})],
            "history_params": result.get("historyParams", {}),
            "version_id": parsed_data.get("version_id"),
        },
        "validateResponse": {
            "alert_flag": parsed_data.get("alert_flag"),
            "configration": parsed_data.get("configuration"),
            "bridgeId": parsed_data.get("bridge_id"),
            "message_id": parsed_data.get("message_id"),
            "org_id": parsed_data.get("org_id"),
        },
        "chatbot_suggestions": {
            "response_format": parsed_data.get("response_format"),
            "assistant": suggestion_content,
            "user": parsed_data.get("user"),
            "bridge_summary": parsed_data.get("bridge_summary"),
            "thread_id": parsed_data.get("thread_id"),
            "sub_thread_id": parsed_data.get("sub_thread_id"),
            "configuration": params.get("configuration", {}),
            "org_id": parsed_data.get("org_id"),
        },
        "handle_gpt_memory" : {
            "id" : parsed_data.get('id'),
            "user" : parsed_data.get('user'),
            "assistant" : result.get('response'),
            "purpose" : parsed_data.get('memory'),
            "gpt_memory_context" : parsed_data.get('gpt_memory_context'),
            "org_id" : parsed_data.get('org_id'),
            "pending_turns": pending_turns,
            "bridge_summary": parsed_data.get("bridge_summary"),
            "thread_id": parsed_data.get("thread_id"),
            "sub_thread_id": parsed_data.get("sub_thread_id"),
            "bridge_id": parsed_data.get("bridge_id"),
            "version_id": parsed_data.get("version_id")
        },
        "check_handle_gpt_memory": {
            "gpt_memory": gpt_memory_enabled and should_fire_gpt_memory,
            "type": parsed_data.get("configuration", {}).get("type"),
        },
        "check_chatbot_suggestions": {
            "bridgeType": parsed_data.get("bridgeType"),
        },
        "save_agent_memory": {
            "user_message": user_message,
            "assistant_message": assistant_message,
            "bridge_id": parsed_data.get("bridge_id"),
            "bridge_name": parsed_data.get("name", ""),
            "system_prompt": parsed_data.get("configuration", {}).get("prompt", ""),
            "cache_on": parsed_data.get("cache_on", False),
            "is_cache_hit": parsed_data.get("is_cache_hit", False),
            "resource_id": parsed_data.get("cache_hit_resource_id", None)
        },
        "type": parsed_data.get("type"),
        "save_files_to_redis": {
            "thread_id": parsed_data.get("thread_id"),
            "sub_thread_id": parsed_data.get("sub_thread_id"),
            "bridge_id": parsed_data.get("bridge_id"),
            "files": parsed_data.get("files"),
        },
        "broadcast_response_webhook": {
            "bridge_id": parsed_data.get("bridge_id"),
            "org_id": parsed_data.get("org_id"),
            "response": result.get("response", {}),
            "user_question": parsed_data.get("user", ""),
            "variables": parsed_data.get("variables", {}),
            "error_type": "broadcast_response",
            "bridge_name": parsed_data.get("name"),
            "is_embed": parsed_data.get("is_embed"),
            "user_id": parsed_data.get("userId"),
            "thread_id": parsed_data.get("thread_id"),
            "service": parsed_data.get("service"),
        },
    }

    return data


def makeFunctionName(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


async def unknown_error_handler_alert(data):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://flow.sokt.io/func/scrimCFAKPWg", json={**data, "env": Config.ENVIRONMENT}) as response:
                return await response.json()
    except Exception as e:
        logger.error(f"unknown_error_handler_alert failed: {e}")

def serialize_config(config) -> dict:
    def default(o):
        if hasattr(o, "model_dump"):
            return o.model_dump()
        return str(o)

    def remove_nulls(obj):
        # Remove None inside dict
        if isinstance(obj, dict):
            return {
                k: remove_nulls(v)
                for k, v in obj.items()
                if v is not None
            }
        # Remove None inside list
        if isinstance(obj, list):
            return [remove_nulls(v) for v in obj if v is not None]
        return obj

    serialized = json.loads(json.dumps(config, default=default))
    return remove_nulls(serialized)


def build_accumulated_response(service, configuration, message_id, accumulated_content,
                                final_tool_calls, final_usage, final_finish_reason, last_delta,
                                service_tier=None, accumulated_reasoning=None):
    """Build a complete response dict from streamed data, matching the shape of each service's non-stream response."""
    full_text = "".join(accumulated_content)
    if service in [service_name["groq"], service_name["grok"], service_name["deepseek"], 
                   service_name["open_router"], service_name["mistral"],
                   service_name["neev_cloud"], service_name["moonshot"]]:
        message = {"role": "assistant", "content": full_text, "tool_calls": final_tool_calls or []}
        if accumulated_reasoning:
            message["reasoning_content"] = "".join(accumulated_reasoning)
        return {
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": final_finish_reason,
            }],
            "model": configuration.get("model", ""),
            "usage": final_usage,
        }
    elif service == service_name["anthropic"]:
        content = [{"type": "text", "text": full_text}] if full_text else []
        if final_tool_calls:
            content += final_tool_calls
        return {
            "id": str(message_id or ""),
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": configuration.get("model", ""),
            "stop_reason": final_finish_reason,
            "stop_sequence": None,
            "usage": final_usage,
        }
    elif service == service_name["gemini"]:
        parts = [{"text": full_text}] if full_text else []
        if final_tool_calls:
            parts += [{"function_call": tc} for tc in final_tool_calls]
        return {
            "candidates": [{"content": {"parts": parts, "role": "model"}, "finish_reason": final_finish_reason}],
            "usage_metadata": final_usage,
        }
    elif service == service_name["openai"]:
        output = list((last_delta or {}).get("output") or [])
        if not output:
            output = [{"type": "message", "content": [{"type": "output_text", "text": full_text}]}]
        if final_tool_calls:
            for tc in final_tool_calls:
                output.append({
                    "type": "function_call",
                    "id": tc.get("id", ""),
                    "call_id": tc.get("call_id") or tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                })
        resp = {"output": output, "model": configuration.get("model", ""), "usage": final_usage, "status": final_finish_reason}
        if service_tier:
            resp["service_tier"] = service_tier
        return resp
    return {"content": full_text, "usage": final_usage, "finish_reason": final_finish_reason}


async def run_stream_and_collect(generator, streamer):
    accumulated_content = []
    accumulated_reasoning = []
    final_tool_calls = None
    final_usage = {}
    final_finish_reason = None
    error_in_stream = None
    last_delta = {}
    service_tier = None

    async for delta in generator:
        last_delta = delta
        if delta.get("error"):
            error_in_stream = delta["error"]
            break
        if delta.get("content"):
            accumulated_content.append(delta["content"])
            await streamer.emit_delta(delta["content"])
        if delta.get("reasoning"):
            accumulated_reasoning.append(delta["reasoning"])
            await streamer.emit_reasoning(delta["reasoning"])
        if delta.get("tool_calls") is not None:
            current_tool_calls = delta["tool_calls"]
            if not delta.get("stream_event_only"):
                final_tool_calls = current_tool_calls
            for tc in current_tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                tool_name = fn.get("name") or tc.get("name", "")
                raw_args = fn.get("arguments", tc.get("args", {}))

                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                is_mcp_event = delta.get("stream_event_only") or tool_name.endswith(MCP_NAME_SUFFIX)
                await streamer.emit_tool_call(
                    name=display_mcp_tool_name(tool_name),
                    args=args,
                    call_id=tc.get("id") or tc.get("call_id", ""),
                    type="mcp" if is_mcp_event else None,
                )
        if delta.get("tool_results") is not None:
            for tool_result in delta["tool_results"]:
                if not isinstance(tool_result, dict):
                    continue
                tool_result_name = tool_result.get("name", "")
                is_mcp_event = delta.get("stream_event_only") or tool_result_name.endswith(MCP_NAME_SUFFIX)
                await streamer.emit_tool_result(
                    name=display_mcp_tool_name(tool_result_name),
                    content=tool_result.get("content", ""),
                    call_id=tool_result.get("call_id", ""),
                    type="mcp" if is_mcp_event else None,
                )
        if delta.get("usage"):
            final_usage = delta["usage"]
        if delta.get("finish_reason"):
            final_finish_reason = delta["finish_reason"]
        if delta.get("service_tier"):
            service_tier = delta["service_tier"]

    return {
        "accumulated_content": accumulated_content,
        "accumulated_reasoning": accumulated_reasoning,
        "final_tool_calls": final_tool_calls,
        "final_usage": final_usage,
        "final_finish_reason": final_finish_reason,
        "error_in_stream": error_in_stream,
        "last_delta": last_delta,
        "service_tier": service_tier,
    }


def remove_additional_properties_with_anyof(schema):
    """
    Remove additionalProperties when anyOf exists at the same level.

    For Anthropic and Mistral, having both anyOf and additionalProperties
    at the same level can cause issues. This removes additionalProperties
    when anyOf is present.

    Args:
        schema (dict): The JSON schema to format

    Returns:
        dict: The formatted schema with additionalProperties removed where anyOf exists
    """
    if not isinstance(schema, dict):
        return schema

    def process_schema(obj):
        """Recursively process schema to remove additionalProperties when anyOf exists."""
        if isinstance(obj, dict):
            # If both anyOf and additionalProperties exist at same level, remove additionalProperties and type
            if 'anyOf' in obj and 'additionalProperties' in obj:
                del obj['additionalProperties']
                del obj['type']

            # Recursively process properties
            if 'properties' in obj and isinstance(obj['properties'], dict):
                for prop_value in obj['properties'].values():
                    process_schema(prop_value)

            # Recursively process items (for arrays)
            if 'items' in obj:
                process_schema(obj['items'])

            # Recursively process additionalProperties
            if 'additionalProperties' in obj and isinstance(obj['additionalProperties'], dict):
                process_schema(obj['additionalProperties'])

            # Recursively process allOf, anyOf, oneOf
            for combiner in ['allOf', 'anyOf', 'oneOf']:
                if combiner in obj and isinstance(obj[combiner], list):
                    for item in obj[combiner]:
                        if isinstance(item, dict):
                            process_schema(item)

            # Process definitions if they exist
            if 'definitions' in obj and isinstance(obj['definitions'], dict):
                for definition in obj['definitions'].values():
                    if isinstance(definition, dict):
                        process_schema(definition)

    process_schema(schema)
    return schema


# ─────────────────────── JSON REPAIR UTILITY ───────────────────────

def fix_json_string(bad_json: str, max_rounds: int = 30) -> str:
    """Attempt to repair a malformed JSON string and return a compact valid JSON string."""
    s = (bad_json or "").strip()
    if not s:
        raise ValueError("Input is empty.")

    parsed = _try_ast_literal(s)
    if parsed is not None:
        return _dump_compact(parsed)

    s = _strip_common_wrappers(s)

    parsed = _try_ast_literal(s)
    if parsed is not None:
        return _dump_compact(parsed)

    s = _repair_common_text_issues(s)
    s = _balance_brackets_and_braces(s)

    for _ in range(max_rounds):
        try:
            return _dump_compact(json.loads(s))
        except json.JSONDecodeError as err:
            new_s = _repair_from_decode_error(s, err)
            if new_s == s:
                break
            s = _balance_brackets_and_braces(new_s)

    final_s = _balance_brackets_and_braces(s)
    try:
        return _dump_compact(json.loads(final_s))
    except Exception:
        return bad_json


def _try_ast_literal(text: str) -> Any:
    """Parse a Python literal (dict/list repr) via ast.literal_eval."""
    try:
        result = ast.literal_eval(text)
        if isinstance(result, (dict, list)):
            return result
        return None
    except Exception:
        return None


def _dump_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _strip_common_wrappers(s: str) -> str:
    """Remove markdown code fences if present."""
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _repair_common_text_issues(s: str) -> str:
    """Fix common JSON formatting mistakes."""
    s = re.sub(r",\s*(\}|\])", r"\1", s)
    s = re.sub(r'(?<!["\w])True(?!["\w])', 'true', s)
    s = re.sub(r'(?<!["\w])False(?!["\w])', 'false', s)
    s = re.sub(r'(?<!["\w])None(?!["\w])', 'null', s)
    s = re.sub(r"(?<!\\)'([A-Za-z0-9_\- ]+)'\s*:", r'"\1":', s)
    s = re.sub(r":\s*'([^'\\]*(?:\\.[^'\\]*)*)'", r':"\1"', s)
    s = re.sub(r"(\[|,)\s*'([^'\\]*(?:\\.[^'\\]*)*)'" , r'\1"\2"', s)
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)
    return s


def _balance_brackets_and_braces(s: str) -> str:
    """Add missing closing brackets/braces and remove unmatched closers."""
    out = []
    stack = []
    in_string = False
    escape = False
    open_to_close = {"{": "}", "[": "]"}

    for ch in s:
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch in open_to_close:
            stack.append(open_to_close[ch])
            out.append(ch)
        elif ch in ("}", "]"):
            if stack and ch == stack[-1]:
                stack.pop()
                out.append(ch)
        else:
            out.append(ch)

    if in_string:
        out.append('"')
    while stack:
        out.append(stack.pop())

    repaired = "".join(out)
    return re.sub(r",\s*(\}|\])", r"\1", repaired)


def _repair_from_decode_error(s: str, err: json.JSONDecodeError) -> str:
    """Apply a targeted fix based on the position reported in a JSONDecodeError."""
    msg = err.msg
    pos = max(0, min(len(s), err.pos))

    def insert_at(text: str, idx: int, token: str) -> str:
        return text[:idx] + token + text[idx:]

    if "Expecting ',' delimiter" in msg:
        return insert_at(s, pos, ",")

    if "Expecting ':' delimiter" in msg:
        return insert_at(s, pos, ":")

    if "Unterminated string" in msg:
        return s + '"'

    if "Expecting property name enclosed in double quotes" in msg:
        candidate = re.sub(r",\s*(\})", r"\1", s)
        if candidate != s:
            return candidate
        if pos < len(s) and s[pos] == "'":
            return s[:pos] + '"' + s[pos + 1:]

    if "Extra data" in msg:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s)
            return _dump_compact(obj)
        except Exception:
            pass

    return _balance_brackets_and_braces(s[:pos].rstrip())
