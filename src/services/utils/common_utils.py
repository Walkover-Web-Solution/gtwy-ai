import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from src.services.utils.built_in_tools.firecrawl import call_firecrawl_scrape
from src.controllers.rag_controller import get_text_from_vectorsQuery
from src.services.utils.ai_call_util import call_ai_middleware
from config import Config
from globals import TRANSFER_HISTORY, BadRequestException, logger, try_catch
from src.configs.model_configuration import model_config_document
from src.configs.serviceKeys import model_config_change
from src.db_services.metrics_service import (
    build_history_and_metrics_payload,
    build_orchestrator_log_data,
    save_conversations_to_redis,
)
from src.services.cache_service import find_in_cache, store_in_cache, make_json_serializable
from src.configs.constant import bridge_ids, redis_keys, alert_types
from src.services.commonServices.baseService.utils import axios_work, make_request_data_and_publish_sub_queue, remove_additional_properties_with_anyof
from src.services.commonServices.queueService.queueLogService import sub_queue_obj
from src.services.commonServices.queueService.queueMetricsService import metrics_queue_obj
from src.services.proxy.Proxyservice import get_timezone_and_org_name
from src.send_alert import send_alert
from src.services.utils.apiservice import fetch
from src.services.utils.time import Timer
from src.services.utils.token_calculation import TokenCalculator
from src.services.utils.update_and_check_cost import update_cost, update_last_used
from src.utils.formatter import apply_variables_to_template_json, fix_json_string
from src.services.utils.helper import Helper

from ...controllers.conversationController import getThread
from ..commonServices.baseService.utils import sendResponse

UTC = timezone.utc

from src.services.utils.rich_text_support import process_chatbot_response
from src.db_services.orchestrator_history_service import orchestrator_collector
from src.services.utils.api_key_status_helper import mark_apikey_status_from_response

def setup_agent_tools(parsed_data, bridge_configurations, tool_data):
    current_bridge_id = parsed_data.get("bridge_id")
    if not current_bridge_id or not bridge_configurations or not tool_data:
        return
    agent_variables = parsed_data.get("variables", {})

    def resolve_args(tool_config, tool_args_mapping, is_custom=False):
        resolved = dict(tool_config)
        for param, var_name in tool_args_mapping.items():
            if var_name in agent_variables:
                resolved[param] = agent_variables[var_name]
            elif not is_custom:
                resolved[param] = var_name
        for param in tool_config.get("required", []):
            if param not in resolved and param in agent_variables:
                resolved[param] = agent_variables[param]
        return resolved

    resolved_tools = []
    if isinstance(tool_data, list):
        tool = tool_data[0]
    else:
        tool_config = tool_data.get("config", {})
        tool_args_mapping = tool_data.get("args", {})
        resolved_args = resolve_args(tool_config, tool_args_mapping)
        return {
            **tool_data,
            "args": resolved_args,
            "config": tool_config
        }
    tool_type = tool.get("_type")
    tool_config = tool.get("config", {})
    tool_args_mapping = tool.get("args", {})
    is_custom = tool_type == "custom_function"
    resolved_args = resolve_args(tool_config, tool_args_mapping, is_custom)
    if is_custom:
        resolved_tools.append({
            "type": "custom_function",
            "name": tool_config.get("script_id"),
            "args": resolved_args,
        })
    else:
        resolved_tools.append({
            "type": tool_type,
            "args": resolved_args,
            "config": tool_config,
        })
    
    return resolved_tools
    
async def handle_agent_transfer(
    result, request_body, bridge_configurations, chat_function, current_bridge_id=None, transfer_request_id=None
):
    transfer_agent_config = result.get("transfer_agent_config")

    # Extract agent_id and user_query
    target_agent_id = transfer_agent_config.get("agent_id")
    user_query = transfer_agent_config.get("user_query")

    logger.info(f"Transfer detected: agent_id={target_agent_id}, user_query={user_query}")

    # Check if target agent exists in bridge_configurations
    if not target_agent_id or target_agent_id not in bridge_configurations:
        logger.warning(f"Transfer agent {target_agent_id} not found in bridge_configurations")
        return None

    # Get the target agent's configuration
    target_agent_config = bridge_configurations[target_agent_id]

    logger.info(f"Transferring to agent: {target_agent_config.get('name', target_agent_id)}")

    # Create a new request body for the transfer agent
    transfer_body = request_body.get("body", {}).copy()
    transfer_body.update(target_agent_config)
    transfer_body["bridge_id"] = target_agent_id
    transfer_body["user"] = user_query

    # Pass the parent_id (current bridge_id) and transfer_request_id to the next agent
    if current_bridge_id:
        transfer_body["parent_bridge_id"] = current_bridge_id
    if transfer_request_id:
        transfer_body["transfer_request_id"] = transfer_request_id

    # Pass complete bridge_configurations so next agent can look up version_ids
    transfer_body["bridge_configurations"] = bridge_configurations

    # Create a complete request structure for the transfer agent
    transfer_request_body = {
        "body": transfer_body,
        "state": request_body.get("state", {}).copy(),
        "path_params": request_body.get("path_params", {}),
    }

    # Call chat function with the transfer agent's data
    transfer_result = await chat_function(transfer_request_body)

    return transfer_result


def parse_request_body(request_body):
    body = request_body.get("body", {})
    state = request_body.get("state", {})
    path_params = request_body.get("path_params", {})

    return {
        "body": body,
        "state": state,
        "path_params": path_params,
        "apikey": body.get("apikey"),
        "bridge_id": path_params.get("bridge_id") or body.get("bridge_id"),
        "configuration": body.get("configuration", {}),
        "thread_id": body.get("thread_id"),
        "sub_thread_id": body.get("sub_thread_id") or body.get("thread_id"),
        "org_id": state.get("profile", {}).get("org", {}).get("id", "") or body.get("org_id"),
        "user": body.get("user"),
        "original_user": body.get("user"),
        "tools": body.get("configuration", {}).get("tools"),
        "service": body.get("service"),
        "wrapper_id": body.get("wrapper_id"),
        "variables": body.get("variables") or {},
        "service_apikeys": body.get("service_apikeys") or {},
        "bridgeType": body.get("chatbot"),
        "template": body.get("template"),
        "settings": body.get("settings", {}),
        "response_format": body.get("settings", {}).get("response_format"),
        "response_type": body.get("configuration", {}).get("response_type"),
        "mode": body.get("mode"),
        "action": body.get("action"),
        "task_id": body.get("task_id"),
        "plans": body.get("plans"),
        "model": body.get("configuration", {}).get("model"),
        "auto_model_select": body.get("auto_model_select", False),
        "is_playground": state.get("is_playground") or body.get("is_playground") or False,
        "bridge": body.get("bridge"),
        "pre_tools": body.get("pre_tools"),
        "version": state.get("version"),
        "fine_tune_model": body.get("configuration", {}).get("fine_tune_model", {}).get("current_model", {}),
        "is_rich_text": body.get("configuration", {}).get("is_rich_text", False),
        "actions": body.get("actions", {}),
        "user_reference": body.get("user_reference", ""),
        "variables_path": body.get("variables_path") or {},
        "tool_id_and_name_mapping": body.get("tool_id_and_name_mapping"),
        "suggest": body.get("suggest", False),
        "message_id": body.get("message_id"),
        "reasoning_model": body.get("configuration", {}).get("model") in {"o1-preview", "o1-mini"},
        "gpt_memory": body.get("gpt_memory"),
        "version_id": body.get("version_id"),
        "gpt_memory_context": body.get("gpt_memory_context"),
        "usage": {},
        "type": body.get("configuration", {}).get("type"),
        "apikey_object_id": body.get("apikey_object_id"),
        "apikey_status": body.get('apikey_status'),
        "is_request_apikey": body.get("is_request_apikey", False),
        "audios": [
            url.get("url")
            for url in body.get("user_urls", [])
            if isinstance(url, dict) and url.get("type") == "audio" and url.get("url")
        ],
        "images": body.get("images")
        or [
            url.get("url")
            for url in body.get("user_urls", [])
            if isinstance(url, dict) and url.get("type") == "image" and url.get("url")
        ],
        "maximum_iterations": body.get("settings", {}).get("maximum_iterations") or 3,
        "tokens": {},
        "memory": "",
        "bridge_summary": body.get("bridge_summary"),
        "batch": body.get("batch") or [],
        "batch_webhook": body.get("webhook"),
        "doc_ids": body.get("ddc_ids"),
        "rag_data": body.get("rag_data"),
        "name": body.get("name"),
        "api_collection": body.get("api_collection"),
        "org_name": body.get("org_name"),
        "variables_state": body.get("variables_state"),
        "built_in_tools": body.get("built_in_tools") or [],
        "thread_flag": body.get("thread_flag") or False,
        "files": body.get("files") or [],
        "fall_back": body.get("settings", {}).get("fall_back") or {},
        "skip_history": body.get("skip_history", False),
        "guardrails": body.get("settings", {}).get("guardrails") or {},
        "testcase_data": body.get("testcase_data") or {},
        "is_embed": body.get("is_embed"),
        "post_tool_data": body.get("post_tool_data"),
        "user_id": body.get("user_id"),
        "file_data": body.get("video_data") or {},
        "youtube_url": body.get("youtube_url") or None,
        "folder_id": body.get("folder_id"),
        "web_search_filters": body.get("web_search_filters") or None,
        "parent_bridge_id": body.get("parent_bridge_id"),
        "transfer_request_id": body.get("transfer_request_id"),
        "orchestrator_flag": body.get("orchestrator_flag"),
        "batch_variables": body.get("batch_variables"),
        "chatbot_auto_answers": body.get("chatbot_auto_answers"),
        "cache_on": body.get("cache_on"),
        "owner_id": state.get("profile", {}).get("owner_id"),
        "richui_templates": body.get("richui_templates", {}),
        "meta": body.get("meta"),
        "limit": body.get("limit"),
        "is_rerun": body.get("is_rerun", False),
    }


def render_template_if_applicable(parsed_data, result):
    response_type = parsed_data.get("response_type", {})
    template_data = None

    if not (isinstance(response_type, dict) and response_type.get("is_template", False)):
        return template_data

    try:
        template_ids = response_type.get("template_id", [])
        if not template_ids:
            logger.warning(
                "Template Rendering: 'is_template' is True but 'template_id' is missing or empty."
            )

        base_template = None
        if template_ids and response_type:
            richui_templates = parsed_data.get("richui_templates", {}) or {}

            ai_data = result.get("response", {}).get("data", {}).get("content", {})
            if isinstance(ai_data, dict) and "item" in ai_data:
                ai_data = ai_data["item"]
            elif isinstance(ai_data, str):
                try:
                    parsed = json.loads(ai_data)
                    if isinstance(parsed, dict) and "item" in parsed:
                        ai_data = parsed["item"]
                    else:
                        ai_data = parsed
                except Exception:
                    try:
                        repaired = fix_json_string(ai_data)
                        ai_data = json.loads(repaired)
                        ai_data = ai_data.get("item")
                    except Exception:
                        pass

            if isinstance(ai_data, dict):
                widget_id = ai_data.get("widget_id")
                if widget_id and str(widget_id) in richui_templates:
                    base_template = richui_templates[str(widget_id)]
                else:
                    logger.warning(
                        f"Template with widget_id '{widget_id}' not found in richui_templates"
                    )

            if base_template:
                try:
                    render_format = base_template.get("template_format", {})
                    render_data = ai_data if isinstance(ai_data, dict) else {}
                    filled_json = apply_variables_to_template_json(render_format, render_data)
                    result.setdefault("response", {}).setdefault("data", {})
                    result["response"]["type"] = "richui_json"
                    result["response"]["data"]["content"] = filled_json
                    result["response"]["data"]["ai_response"] = render_data

                    template_data = {
                        "template_id": base_template.get("_id") or base_template.get("id"),
                        "template_name": base_template.get("name"),
                        "is_template": True,
                    }
                except Exception as render_err:
                    logger.error(f"Template Rendering Failed: {render_err}")
            else:
                logger.info("No matching template found, returning data as-is")
    except Exception as exc:
        logger.error(f"Error rendering template: {str(exc)}")

    return template_data


async def apply_prompt_wrapper(parsed_data):
    """
    Apply prompt wrapper overrides when a valid wrapper_id is present.
    """
    wrapper_id = parsed_data.get("wrapper_id") or parsed_data.get("body", {}).get("wrapper_id")
    if not wrapper_id:
        return

    wrapper_doc = await ConfigurationService.get_prompt_wrapper_by_id(str(wrapper_id), parsed_data.get("org_id"))
    if not wrapper_doc:
        return

    wrapper_template = wrapper_doc.get("template")
    config_prompt = parsed_data["configuration"].get("prompt", "")

    template_context = {"prompt": config_prompt, **parsed_data.get("variables", {})}


    final_prompt = None
    if wrapper_template:
        final_prompt, _ = Helper.replace_variables_in_prompt(wrapper_template, template_context)
        parsed_data["configuration"]["prompt"] = final_prompt


def convert_prompt_to_string(prompt):

    if isinstance(prompt, dict):
        parts = []
        
        # Add role if present
        if prompt.get("role"):
            parts.append(f"Role: {prompt['role']}")
        
        # Add goal if present
        if prompt.get("goal"):
            parts.append(f"Goal: {prompt['goal']}")
        
        # Add instructions if present
        instruction_value = prompt.get("instruction")
        if instruction_value:
            parts.append(f"Instructions: {instruction_value}")
                
        return "\n\n".join(parts)
    
    if prompt is None:
        return ""
    
    return str( prompt)


def add_default_template(prompt):
    suffix = " \n ### CURRENT TIME (For reference only) \n{{current_time_date_and_current_identifier}}"
    
    if isinstance(prompt, dict):
        if prompt.get("customPrompt"):
             prompt["customPrompt"] += suffix
        elif prompt.get("instruction"):
             prompt["instruction"] += suffix

    else:
        # String case
        prompt = convert_prompt_to_string(prompt) + suffix

    return prompt


def add_user_in_variables(variables, user):
    variables["_user_message"] = user
    return variables


def initialize_timer(state: dict[str, Any]) -> Timer:
    timer_obj = Timer()
    timer_obj.defaultStart(state.get("timer", []))
    return timer_obj


async def load_model_configuration(model, configuration, service):
    model_obj = model_config_document[service][model]
    if not model_obj:
        raise BadRequestException(f"Model {model} not found in ModelsConfig.")

    # model_obj = modelfunc()
    model_config = model_obj["configuration"]
    model_output_config = model_obj["outputConfig"]

    custom_config = {}
    for key, config in model_config.items():
        if key == "type" or key == "specification":
            continue
        if (
            "level" in config
            and (config["level"] == 0 or config["level"] == 1 or config["level"] == 2)
            or key in configuration
        ):
            if config.get("level") == 0 and key not in configuration:
                continue
            if configuration.get(key):
                custom_config[key] = configuration[key]

    return model_obj, custom_config, model_output_config


async def handle_fine_tune_model(parsed_data, custom_config):
    if (
        parsed_data["configuration"]["type"] == "chat"
        and parsed_data["fine_tune_model"]
        and parsed_data["model"] in {"gpt-4o-mini-2024-07-18", "gpt-4o-2024-08-06", "gpt-4-0613"}
    ):
        custom_config["model"] = parsed_data["fine_tune_model"]


async def handle_pre_tools(parsed_data, custom_config):
    pre_tools = parsed_data.get("pre_tools") or []
    if not pre_tools:
        return

    for tool in pre_tools:
        tool_type = tool.get("type")
        args = dict(tool.get("args", {}))
        args["user"] = parsed_data["user"]
        args["_response_type"] = parsed_data["configuration"]["response_type"]

        if tool_type == "custom_function":
            pre_function_response = await axios_work(
                args,
                {"url": f"https://flow.sokt.io/func/{tool.get('name')}"},
            )
            if pre_function_response.get("status") == 0:
                parsed_data["variables"]["pre_function"] = (
                    f"Error while calling prefunction. Error message: {pre_function_response.get('response')}"
                )
            else:
                parsed_data["variables"]["pre_function"] = pre_function_response.get("response")
                response_data = pre_function_response.get("response", {})
                Helper.update_agentconfig_from_pre_function(response_data, parsed_data, custom_config)
        
        elif tool_type == "query_refiner":
            prompt = args.get("prompt", "")
            user_query = parsed_data["user"]
            variables = {**parsed_data.get("variables", {})}
            if prompt:
                variables["prompt"] = prompt
            try:
                optimised_query = await call_ai_middleware(
                    user=user_query,
                    bridge_id=bridge_ids["query_refiner"],
                    variables=variables,
                    response_type="text",
                )
                optimised_query = optimised_query or user_query
            except Exception as e:
                optimised_query = user_query
         
            parsed_data["user"] = optimised_query
            
        elif tool_type == "rag_knowledgebase":
            resource_id = args.get("resource_id")
            collection_id = args.get("collection_id")
            owner_id = parsed_data.get("owner_id")
            resource_to_collection_mapping = {resource_id: collection_id} if resource_id and collection_id else {}
            if not resource_id or not collection_id:
                parsed_data["variables"]["rag_pre_result"] = ""
                logger.warning(f"rag_knowledgebase pre-tool missing resource_id or collection_id for bridge {parsed_data.get('bridge_id')}")
                continue
            rag_response = await get_text_from_vectorsQuery(
                {
                    "resource_id": resource_id,
                    "query": parsed_data["user"],
                    "top_k": args.get("top_k", 3),
                    "minScore": args.get("minScore", 0.1),
                },
                Flag=True,
                owner_id=owner_id,
                resource_to_collection_mapping=resource_to_collection_mapping,
            )
            if rag_response.get("status") == 1:
                parsed_data["variables"]["rag_pre_result"] = rag_response.get("response")
            else:
                parsed_data["variables"]["rag_pre_result"] = f"Error: {rag_response.get('response', 'unknown error')}"
        
        elif tool_type == "gtwy_web_search":
            web_response = await call_firecrawl_scrape(args)
            if web_response.get("status") == 1:
                parsed_data["variables"]["web_search_pre_result"] = web_response.get("response")
            else:
                response = web_response.get('response')
                error_msg = f"Error: {response.get('error', 'unknown error') if isinstance(response, dict) else response or 'unknown error'}"
                parsed_data["variables"]["web_search_pre_result"] = error_msg


async def handle_post_tool(parsed_data, result):
    """Execute the folder-level post_tool after every AI call."""
    post_tool_data = parsed_data.get("post_tool_data")
    if not post_tool_data:
        return

    script_id = post_tool_data.get("script_id") or post_tool_data.get("function_name") or post_tool_data.get("endpoint_name")
    if not script_id:
        logger.warning("post_tool configured but no script_id / function_name found; skipping")
        return

    try:
        args = {
            **dict(post_tool_data.get("args", {})),
            "user": parsed_data.get("user"),
            "_response_type": parsed_data.get("configuration", {}).get("response_type"),
            "bridge_id": parsed_data.get("bridge_id"),
            "thread_id": parsed_data.get("thread_id"),
            "org_id": parsed_data.get("org_id"),
        }
        response_data = (result or {}).get("response", {}).get("data") if isinstance(result, dict) else None
        if response_data:
            args["ai_response"] = response_data.get("content") or response_data

        post_tool_response = await axios_work(
            args,
            {"url": f"https://flow.sokt.io/func/{script_id}"},
        )
    except Exception as err:
        logger.error(f"post_tool execution error (script_id={script_id}): {err}")
        return

    flow_hit_id = (post_tool_response.get("metadata") or {}).get("flowHitId")
    entry = {
        "id": post_tool_data.get("script_id"),
        "args": post_tool_data.get("args", {}),
        "data": post_tool_response,
        "type": "post_tool",
        "name": post_tool_data.get("title") or post_tool_data.get("script_id"),
        "error": post_tool_response.get("status") != 1,
    }
    post_tool_log = {flow_hit_id: entry} if flow_hit_id else entry
    if result is not None and result.get("historyParams") is not None:
        result["historyParams"].setdefault("tools_call_data", []).append(post_tool_log)
    if post_tool_response.get("status") == 1 and post_tool_response.get("response") is not None:
        if isinstance(result, dict) and result.get("response", {}).get("data") is not None:
            result["response"]["data"]["content"] = post_tool_response.get("response")

async def manage_threads(parsed_data):
    thread_id = parsed_data["thread_id"]
    sub_thread_id = parsed_data["sub_thread_id"]
    bridge_id = parsed_data["bridge_id"]
    org_id = parsed_data["org_id"]

    if thread_id:
        thread_id = thread_id.strip()

        # Ensure sub_thread_id is set when thread_id exists but sub_thread_id is missing
        # This handles A2A calls where parent passes thread_id but child's sub_thread_id may be None
        if not sub_thread_id:
            sub_thread_id = thread_id
            parsed_data["sub_thread_id"] = sub_thread_id

        # Check Redis cache first for conversations
        version_id = parsed_data.get("version_id", "")
        redis_key = f"conversation_{version_id}_{thread_id}_{sub_thread_id}"
        cached_conversations = await find_in_cache(redis_key)

        if cached_conversations:
            # Use cached conversations from Redis
            parsed_data["configuration"]["conversation"] = json.loads(cached_conversations)
            result = json.loads(cached_conversations)
            logger.info(f"Retrieved conversations from Redis cache: {redis_key}")
        else:
            # Fallback to database if not in cache
            result = await try_catch(getThread, thread_id, sub_thread_id, org_id, bridge_id)
            if result:
                parsed_data["configuration"]["conversation"] = result or []
    else:
        thread_id = str(uuid.uuid1())
        sub_thread_id = thread_id
        parsed_data["thread_id"] = thread_id
        parsed_data["sub_thread_id"] = sub_thread_id
        parsed_data["gpt_memory"] = False
        result = []

    # cache_key = f"{bridge_id}_{thread_id}_{sub_thread_id}"
    # if len(parsed_data['files']) == 0:
    #     cached_files = await find_in_cache(cache_key)
    #     if cached_files:
    #         parsed_data['files'] = json.loads(cached_files)

    return {"thread_id": thread_id, "sub_thread_id": sub_thread_id, "result": result}


def process_variable_state(parsed_data):
    """
    Check and add default values for variables based on variable_state.

    Args:
        parsed_data: Dictionary containing the request data

    Expected variable_state structure:
        {
            'var_name': {
                'status': 'required',
                'default_value': 'some_default',
                'value': ''
            }
        }

    Returns:
        None (modifies parsed_data in place)
    """
    if "variables_state" in parsed_data and parsed_data["variables_state"] is not None:
        for var_name, var_state in parsed_data["variables_state"].items():
            if isinstance(var_state, dict) and "status" in var_state and "default_value" in var_state:
                # Check if variable doesn't exist, is empty/None, or if the value in variable_state is empty
                current_value = parsed_data["variables"].get(var_name)

                # Use default_value if:
                # 1. Variable doesn't exist in variables
                # 2. Variable exists but is None or empty string
                # 3. Variable_state has empty value
                if (current_value is None or current_value == "" or var_name not in parsed_data["variables"]) and var_state["default_value"] not in (None, ""):
                    parsed_data["variables"][var_name] = var_state["default_value"]


async def prepare_prompt(parsed_data, thread_info, model_config, custom_config):
    configuration = parsed_data["configuration"]
    variables = parsed_data["variables"]
    template = parsed_data["template"]
    bridge_type = parsed_data["bridgeType"]
    suggest = parsed_data["suggest"]
    gpt_memory = parsed_data["gpt_memory"]
    memory = None

    if configuration["type"] == "chat" or configuration["type"] == "reasoning":
        id = f"{thread_info['thread_id']}_{thread_info['sub_thread_id']}_{parsed_data.get('version_id') or parsed_data.get('bridge_id')}"
        parsed_data["id"] = id
        if gpt_memory:
            memory = await find_in_cache(id)
            if memory:
                # Convert bytes to string if needed
                if isinstance(memory, bytes):
                    memory = memory.decode("utf-8")
                parsed_data["memory"] = memory
            else:
                response, _ = await fetch(
                    "https://flow.sokt.io/func/scriCJLHynCG", "POST", None, None, {"threadID": id}
                )
                parsed_data["memory"] = response
                memory = response
        configuration["prompt"], missing_vars = Helper.replace_variables_in_prompt(
            configuration.get("prompt") or "", variables
        )

        if template:
            system_prompt = template
            configuration["prompt"], missing_vars = Helper.replace_variables_in_prompt(
                system_prompt, {"system_prompt": configuration["prompt"], **variables}
            )

        if bridge_type and model_config.get("response_type") and suggest:
            template_content = (await ConfigurationService.get_template_by_id(Config.CHATBOT_OPTIONS_TEMPLATE_ID)).get(
                "template", ""
            )
            configuration["prompt"], missing_vars = Helper.replace_variables_in_prompt(
                template_content, {"system_prompt": configuration["prompt"]}
            )
            custom_config["response_type"] = {"type": "json_object"}

        if not parsed_data["is_playground"] and bridge_type is None and model_config.get("response_type"):
            res = parsed_data["body"].get("response_type") or parsed_data["body"].get("configuration", {}).get(
                "response_type", {"type": "json_object"}
            )
            match res:
                case "default":
                    custom_config["response_type"] = {"type": "json_object"}
                case "text":
                    custom_config["response_type"] = {"type": "text"}
                case _:
                    custom_config["response_type"] = res
        if parsed_data["bridge_summary"] is not None:
            parsed_data["bridge_summary"], missing_vars = Helper.replace_variables_in_prompt(
                parsed_data["bridge_summary"], variables
            )
        return memory, missing_vars

    return memory, []


async def configure_custom_settings(model_configuration, custom_config, service):
    return await model_config_change(model_configuration, custom_config, service)


def build_service_params(
    parsed_data,
    custom_config,
    model_output_config,
    thread_info=None,
    timer=None,
    memory=None,
    bridge_configurations=None,
):
    token_calculator = TokenCalculator(parsed_data["service"], model_output_config)

    return {
        "customConfig": custom_config,
        "configuration": parsed_data["configuration"],
        "apikey": parsed_data["apikey"],
        "variables": parsed_data["variables"],
        "user": parsed_data["user"],
        "original_user": parsed_data["original_user"],
        "org_id": parsed_data["org_id"],
        "bridge_id": parsed_data["bridge_id"],
        "bridge": parsed_data["bridge"],
        "thread_id": thread_info["thread_id"] if thread_info else parsed_data["thread_id"],
        "sub_thread_id": thread_info["sub_thread_id"] if thread_info else parsed_data["sub_thread_id"],
        "model": parsed_data["model"],
        "service": parsed_data["service"],
        "modelOutputConfig": model_output_config,
        "playground": parsed_data["is_playground"],
        "template": parsed_data["template"],
        "response_format": parsed_data.get("response_format") or {},
        "thread_flag": parsed_data.get("thread_flag"),
        "execution_time_logs": parsed_data.get("execution_time_logs", []),
        "function_time_logs": [],
        "timer": timer,
        "variables_path": parsed_data["variables_path"],
        "message_id": parsed_data["message_id"],
        "bridgeType": parsed_data["bridgeType"],
        "tool_id_and_name_mapping": parsed_data["tool_id_and_name_mapping"],
        "reasoning_model": parsed_data["reasoning_model"],
        "memory": memory,
        "type": parsed_data["configuration"].get("type"),
        "token_calculator": token_calculator,
        "apikey_object_id": parsed_data["apikey_object_id"],
        "images": parsed_data["images"],
        "audios": parsed_data.get("audios"),
        "maximum_iterations": parsed_data["maximum_iterations"],
        "rag_data": parsed_data["rag_data"],
        "name": parsed_data["name"],
        "org_name": parsed_data["org_name"],
        "built_in_tools": parsed_data["built_in_tools"],
        "files": parsed_data["files"],
        "file_data": parsed_data["file_data"],
        "youtube_url": parsed_data["youtube_url"],
        "web_search_filters": parsed_data["web_search_filters"],
        "folder_id": parsed_data.get("folder_id"),
        "bridge_configurations": bridge_configurations,
        "owner_id": parsed_data.get("owner_id"),
        "limit": parsed_data.get("limit"),
        "is_embed": parsed_data.get("is_embed"),
        "user_id": parsed_data.get("user_id"),
        "api_collection": parsed_data.get("api_collection"),
        "meta": parsed_data.get("meta"),
    }


def _attach_sub_thread_extras(conversation_log_data, parsed_data):
    conversation_log_data["thread_flag"] = parsed_data.get("thread_flag")
    conversation_log_data["response_format"] = parsed_data.get("response_format")


def _build_orchestrator_sub_thread_data(parsed_data, thread_info=None):
    return {
        "org_id": parsed_data.get("org_id"),
        "thread_id": (thread_info or {}).get("thread_id") or parsed_data.get("thread_id"),
        "sub_thread_id": (thread_info or {}).get("sub_thread_id") or parsed_data.get("sub_thread_id"),
        "thread_flag": parsed_data.get("thread_flag"),
        "response_format": parsed_data.get("response_format"),
        "bridge_id": parsed_data.get("bridge_id"),
        "user": parsed_data.get("user"),
    }


async def _publish_history_to_queue(dataset, history_params, version_id, thread_info=None, parsed_data=None):
    """Build history/metrics payload and publish it to the log queue for Node.js to save."""
    try:
        payload = build_history_and_metrics_payload(dataset, history_params, version_id)
        if parsed_data is not None:
            _attach_sub_thread_extras(payload["conversation_log_data"], parsed_data)
        metrics_data = payload["metrics_data"]
        history_data = payload["conversation_log_data"]
        await sub_queue_obj.publish_message(make_json_serializable({"save_history": [history_data]}))
        await metrics_queue_obj.publish_message(make_json_serializable({"save_metrics": metrics_data}))

        asyncio.create_task(_send_history_to_rt_layer(history_params))
    except Exception as err:
        logger.error(f"Error publishing history/metrics to queue: {str(err)}")


async def _send_history_to_rt_layer(history_entry):
    if not history_entry.get("bridge_id"):
        return

    org_id = str(history_entry.get("org_id") or "")
    response_format_copy = {
        "cred": {
            "channel": org_id + "_" + (history_entry.get("bridge_id") or ""),
            "apikey": Config.RTLAYER_AUTH,
            "ttl": "1",
        },
        "type": "RTLayer",
    }
    await sendResponse(response_format_copy, history_entry, True)


async def _update_history_redis(dataset, history_params, version_id, thread_info):
    """
    Handle Python-side Redis updates after history data is built.
    Keeps conversation cache and token count cache in sync.
    """
    if thread_info is None:
        thread_info = {}

    thread_id = thread_info.get("thread_id")
    sub_thread_id = thread_info.get("sub_thread_id")
    conversations = thread_info.get("result", [])

    if dataset and "error" not in dataset[0] and conversations:
        await save_conversations_to_redis(conversations, version_id, thread_id, sub_thread_id, history_params)

    if history_params and history_params.get("bridge_id"):
        cache_key = f"{redis_keys['metrix_bridges_']}{history_params['bridge_id']}"
        cache_value = await find_in_cache(cache_key)
        try:
            old_total = json.loads(cache_value) if cache_value else 0
        except (json.JSONDecodeError, TypeError):
            old_total = 0
        total_tokens = sum(d.get("total_tokens", 0) for d in (dataset or [])) + old_total
        await store_in_cache(cache_key, float(total_tokens))


async def _save_plan_from_result(parsed_data, result):
    """Parse the planner LLM JSON out of `result` and persist the plan to Redis.

    Safety guarantees:
    - On replan, merge new tasks with existing plan so completed / answered
      tasks are never lost even if the LLM forgets to re-emit them.
    - The original goal is preserved once an existing plan is present.
    - If the new output is malformed or contains no tasks while an existing
      plan has tasks, the existing plan is kept intact (never wiped).
    - Raw content is logged on any failure path so issues are debuggable.

    Also attaches the parsed plan to `historyParams["plans"]` so the history
    row carries the same `plans` field todo_handler expects.
    """
    from src.services.todo import plan_store
    from src.services.todo.planner_service import _parse_plan_json

    org_id = parsed_data["org_id"]
    bridge_id = parsed_data["bridge_id"]
    thread_id = parsed_data["thread_id"]
    sub_thread_id = parsed_data.get("sub_thread_id") or thread_id

    try:
        response = (result.get("historyParams") or {}).get("response") or result.get("response") or {}
        content = (response.get("data") or {}).get("content") or ""
        if not content:
            logger.warning(
                f"Planner returned empty content for thread={thread_id}/sub={sub_thread_id}; keeping any existing plan."
            )
            return

        try:
            plan_json = _parse_plan_json(content)
        except Exception as parse_err:
            logger.error(
                f"Planner JSON parse failed for thread={thread_id}/sub={sub_thread_id}: {parse_err}. "
                f"Keeping existing plan if any. Raw content (truncated): {content[:1000]}"
            )
            return

        if not isinstance(plan_json, dict):
            logger.error(
                f"Planner output is not an object for thread={thread_id}/sub={sub_thread_id}. Keeping existing plan. Got type={type(plan_json).__name__}"
            )
            return

        plan_json["org_id"] = org_id
        plan_json["bridge_id"] = bridge_id
        plan_json["thread_id"] = thread_id
        plan_json["sub_thread_id"] = sub_thread_id
        plan_json["message_id"] = parsed_data.get("message_id", "")

        await plan_store.save_plan(plan_json)
        logger.info(f"Plan saved for thread={thread_id}/sub={sub_thread_id}.")
        if result.get("historyParams") is not None:
            result["historyParams"]["plans"] = plan_json
    except Exception as err:
        logger.error(
            f"Failed to save plan from chat result for thread={thread_id}/sub={sub_thread_id}: {err}",
            exc_info=True,
        )


async def process_background_tasks(
    parsed_data, result, params, thread_info, transfer_request_id=None, bridge_configurations=None
):
    """
    Process background tasks for saving history and publishing to queue.
    Handles both regular flow and transfer chain scenarios.
    Also handles orchestrator mode where multiple agents are saved in a single entry.
    History and metrics are now published to the log queue for Node.js to save to DB.
    """
    # Primary-agent sub-tasks inside plan mode skip per-call history; the
    # single final plan result is saved by todo_handler after full execution.
    if parsed_data.get("skip_history"):
        return

    # Plan mode: parse the LLM JSON, save to Redis, and thread the parsed plan
    # into historyParams so the conversation log persists `plans`.
    if parsed_data.get("mode") == "plan" and not parsed_data.get("action"):
        await _save_plan_from_result(parsed_data, result)

    orchestrator_flag = parsed_data.get("orchestrator_flag") or parsed_data.get("body", {}).get("orchestrator_flag")

    is_transfer_chain = (
        transfer_request_id
        and transfer_request_id in TRANSFER_HISTORY
        and len(TRANSFER_HISTORY[transfer_request_id]) > 0
    )

    history_entries = []
    metrics_entries = []
    orchestrator_history_data = None

    if is_transfer_chain:
        bridge_configs = bridge_configurations or {}
        final_version_id = bridge_configs.get(parsed_data["bridge_id"], {}).get("version_id", parsed_data["version_id"])

        current_history_data = {
            "bridge_id": parsed_data["bridge_id"],
            "history_params": result.get("historyParams"),
            "dataset": [parsed_data["usage"]],
            "version_id": final_version_id,
            "thread_info": thread_info,
            "parent_id": parsed_data.get("parent_bridge_id", ""),
        }
        TRANSFER_HISTORY[transfer_request_id].append(current_history_data)

        transfer_chain = TRANSFER_HISTORY[transfer_request_id]

        if orchestrator_flag:
            for _idx, history_entry in enumerate(transfer_chain):
                if history_entry["history_params"]:
                    agent_bridge_id = history_entry["bridge_id"]
                    if bridge_configs and agent_bridge_id in bridge_configs:
                        agent_config = bridge_configs[agent_bridge_id].get("configuration", {})
                        history_entry["history_params"]["prompt"] = agent_config.get("prompt")

            orchestrator_history_data = build_orchestrator_log_data(transfer_chain, thread_info)
        else:
            for idx, history_entry in enumerate(transfer_chain):
                if history_entry["history_params"]:
                    history_entry["history_params"]["parent_id"] = history_entry.get("parent_id", "")

                    if idx < len(transfer_chain) - 1:
                        history_entry["history_params"]["child_id"] = transfer_chain[idx + 1]["bridge_id"]
                    else:
                        history_entry["history_params"]["child_id"] = None

                    agent_bridge_id = history_entry["bridge_id"]
                    if bridge_configs and agent_bridge_id in bridge_configs:
                        agent_config = bridge_configs[agent_bridge_id].get("configuration", {})
                        history_entry["history_params"]["prompt"] = agent_config.get("prompt")

                payload = build_history_and_metrics_payload(
                    history_entry["dataset"],
                    history_entry["history_params"],
                    history_entry["version_id"],
                )
                metrics_entries.extend(payload["metrics_data"])
                history_entries.append(payload["conversation_log_data"])

                asyncio.create_task(
                    _update_history_redis(
                        history_entry["dataset"],
                        history_entry["history_params"],
                        history_entry["version_id"],
                        history_entry["thread_info"],
                    )
                )

        del TRANSFER_HISTORY[transfer_request_id]
    else:
        if result.get("historyParams"):
            result["historyParams"]["parent_id"] = parsed_data.get("parent_bridge_id", "")
            result["historyParams"]["child_id"] = None

        payload = build_history_and_metrics_payload(
            [parsed_data["usage"]],
            result["historyParams"],
            parsed_data["version_id"],
        )

        metrics_entries.extend(payload["metrics_data"])
        history_entries.append(payload["conversation_log_data"])

        asyncio.create_task(
            _update_history_redis(
                [parsed_data["usage"]],
                result["historyParams"],
                parsed_data["version_id"],
                thread_info,
            )
        )

    data = await make_request_data_and_publish_sub_queue(parsed_data, result, params, thread_info)

    if history_entries:
        _attach_sub_thread_extras(history_entries[0], parsed_data)
        data["save_history"] = history_entries

        asyncio.gather(
            *(_send_history_to_rt_layer(history_entry) for history_entry in history_entries),
            return_exceptions=True
        )

    if orchestrator_history_data:
        orchestrator_history_data["sub_thread_data"] = _build_orchestrator_sub_thread_data(parsed_data, thread_info)
        data["save_orchestrator_history"] = orchestrator_history_data

    data = make_json_serializable(data)
    await sub_queue_obj.publish_message(data)

    await metrics_queue_obj.publish_message(make_json_serializable({"save_metrics": metrics_entries}))

async def process_background_tasks_for_error(parsed_data, error):
    # Primary-agent sub-tasks inside plan mode skip per-call history.
    if parsed_data.get("skip_history"):
        return

    tasks = [
        send_alert(
            bridge_id=parsed_data["bridge_id"],
            org_id=parsed_data["org_id"],
            error_log={"error": str(error), "message": "Exception for the code", "message_id": parsed_data["message_id"]},
            error_type=alert_types["error"],
            bridge_name=parsed_data.get("name"),
            org_name=parsed_data.get("org_name"),
            is_embed=parsed_data.get("is_embed"),
            user_id=parsed_data.get("user_id"),
            thread_id=parsed_data.get("thread_id"),
            service=parsed_data.get("service"),
            is_playground=parsed_data.get("is_playground"),
            api_collection=parsed_data.get("api_collection"),
            is_external_error=False,
        ),
        _publish_history_to_queue(
            [parsed_data["usage"]],
            parsed_data["historyParams"],
            parsed_data["version_id"],
            parsed_data=parsed_data,
        ),
    ]
    await asyncio.gather(*[task for task in tasks if task is not None], return_exceptions=True)


async def process_batch_background_tasks(parsed_data, result, processed_prompts, batch_variables):
    """
    Process background tasks for batch API including conversation log creation and subthread saving.
    
    Args:
        parsed_data: Parsed request data
        result: Result from batch execution containing batch_id and messages
        processed_prompts: List of processed prompts for each batch message
        batch_variables: List of variables for each batch message
    """
    from src.db_services.metrics_service import create_batch_conversation_logs
    
    batch_id = result.get("batch_id")
    messages = result.get("messages", [])
    
    tasks = []

    if batch_id and messages:
        tasks.append(
            create_batch_conversation_logs(
                batch_id=batch_id,
                messages=messages,
                parsed_data=parsed_data,
                processed_prompts=processed_prompts,
                batch_variables=batch_variables,
            )
        )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

def build_service_params_for_batch(parsed_data, custom_config, model_output_config):

    # Temporary fix for batch - stream bug
    custom_config.pop("stream", None)

    return {
        "customConfig": custom_config,
        "configuration": parsed_data["configuration"],
        "apikey": parsed_data["apikey"],
        "variables": parsed_data["variables"],
        "user": parsed_data["user"],
        "tools": parsed_data["tools"],
        "org_id": parsed_data["org_id"],
        "bridge_id": parsed_data["bridge_id"],
        "bridge": parsed_data["bridge"],
        "model": parsed_data["model"],
        "service": parsed_data["service"],
        "modelOutputConfig": model_output_config,
        "playground": parsed_data["is_playground"],
        "template": parsed_data["template"],
        "response_format": parsed_data["response_format"],
        "thread_flag": parsed_data.get("thread_flag"),
        "execution_time_logs": [],
        "variables_path": parsed_data["variables_path"],
        "message_id": parsed_data["message_id"],
        "bridgeType": parsed_data["bridgeType"],
        "reasoning_model": parsed_data["reasoning_model"],
        "type": parsed_data["configuration"].get("type"),
        "apikey_object_id": parsed_data["apikey_object_id"],
        "batch": parsed_data["batch"],
        "webhook": parsed_data["batch_webhook"],
        "folder_id": parsed_data.get("folder_id"),
        "batch_variables": parsed_data["batch_variables"],
        "processed_prompts": parsed_data.get("processed_prompts", []),
        "thread_id": parsed_data.get("thread_id"),
        "sub_thread_id": parsed_data.get("sub_thread_id"),
        "gpt_memory_context": parsed_data.get("gpt_memory_context", ""),
        "files": parsed_data.get("files", []),
        "version_id": parsed_data.get("version_id", ""),
        "meta": parsed_data.get("meta"),
    }


async def updateVariablesWithTimeZone(variables, org_id):
    org_name = ""

    async def getTimezoneOfOrg():
        data = await get_timezone_and_org_name(org_id)
        timezone = data.get("timezone") or "+5:30"
        hour, minutes = timezone.split(":")
        return int(hour), int(minutes), data.get("name") or "", (data.get("meta") or {}).get("identifier", "")

    hour, minutes, org_name, identifier = await getTimezoneOfOrg()
    if "timezone" in variables and variables["timezone"]:
        hour, minutes = Helper.get_current_time_with_timezone(variables["timezone"])
        identifier = variables["timezone"]
    current_time = datetime.now(UTC)
    current_time = current_time + timedelta(hours=hour, minutes=minutes)
    if identifier == "" and "timezone" not in variables:
        identifier = "Asia/Calcutta"
    variables["current_time_date_and_current_identifier"] = (
        current_time.strftime("%Y-%m-%d")
        + " "
        + current_time.strftime("%H:%M:%S")
        + " "
        + current_time.strftime("%A")
        + " ("
        + identifier
        + ")"
    )
    return variables, org_name


def filter_missing_vars(missing_vars, variables_state):
    # Handle if variables_state is None
    if variables_state is None:
        return missing_vars

    # Iterate through keys in missing_vars
    keys_to_remove = [key for key, value in variables_state.items() if value != "required"]

    # Remove the keys from missing_vars that are in the keys_to_remove list
    for key in keys_to_remove:
        if key in missing_vars:
            del missing_vars[key]

    return missing_vars


def get_service_by_model(model):
    return next((s for s in model_config_document if model in model_config_document[s]), None)


def restructure_json_schema(response_type, service):
    # Handle Template IDs -> Generate Schema
    if 'template_id' in response_type:
        template_ids = response_type['template_id']
        if not isinstance(template_ids, list):
            template_ids = [template_ids]
            
        schemas = []        
        if schemas:
             if len(schemas) == 1:
                 response_type['json_schema'] = schemas[0]
             else:
                 response_type['json_schema'] = {
                     "name": "ui_components_response", # Generic name
                     "strict": True,
                     "schema": {
                         "type": "object",
                         "properties": {
                             "item": {
                                 "anyOf": [s['schema'] for s in schemas]
                             }
                         },
                         "required": ["item"],
                         "additionalProperties": False
                     }
                 }
    
    match service:
        case "openai":
            schema = response_type.pop("json_schema", {})
            for key, value in schema.items():
                response_type[key] = value
            return response_type
        case "anthropic":
            # ServiceKeys renames response_type -> output_config; value must be API output_config payload (no extra nesting)
            json_schema = response_type.get("json_schema") or {}
            if not isinstance(json_schema, dict):
                return response_type
            schema = json_schema.get("schema") if "schema" in json_schema else json_schema
            if not schema or not isinstance(schema, dict):
                return response_type
            schema = remove_additional_properties_with_anyof(schema)
            return {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            }
        case _:
            return response_type


def validate_json_schema_configuration(configuration):
    """
    Validates the JSON schema configuration for response_type.
    Only validates when 'response_type' key is present in configuration.

    Args:
        configuration (dict): The configuration object to validate

    Returns:
        tuple: (is_valid, error_message)

    Raises:
        None - returns validation result as tuple
    """
    if not configuration or "response_type" not in configuration:
        return True, None

    response_type = configuration.get("response_type")
    if not response_type:
        return True, None

    # If response_type is a string (like "default"), allow it
    if isinstance(response_type, str):
        return True, None

    # Check if type is json_schema
    if response_type.get("type") != "json_schema":
        return True, None

    # If json_schema key exists and is None, return error
    if "json_schema" in response_type and response_type["json_schema"] is None:
        return False, "json_schema should be a valid JSON, not None"

    # If json_schema key exists and is not None, validate it's valid JSON
    if "json_schema" in response_type and response_type["json_schema"] is not None:
        try:
            # If it's already a dict/object, it's valid
            if isinstance(response_type["json_schema"], dict):
                return True, None
            # If it's a string, try to parse it as JSON
            elif isinstance(response_type["json_schema"], str):
                json.loads(response_type["json_schema"])
                return True, None
            else:
                return False, "json_schema should be a valid JSON object or string"
        except (json.JSONDecodeError, TypeError):
            return False, "json_schema should be a valid JSON"

    # If json_schema key is not present, it's an error — APIs require the schema body when type is json_schema
    return False, "json_schema field is required when response_type.type is 'json_schema'"


def create_latency_object(timer, params):
    """
    Create a latency metrics object for API usage tracking.

    Args:
        timer: Timer object for tracking execution time
        params: Parameters dictionary containing execution logs

    Returns:
        Dictionary containing latency metrics
    """
    # Safely get overall time without overriding original errors
    over_all_time = 0.00
    try:
        if hasattr(timer, "start_times") and timer.start_times:
            over_all_time = timer.stop("Api total time")
    except Exception:
        # Silently fail to avoid overriding original error
        pass

    return {
        "over_all_time": over_all_time,
        "model_execution_time": sum([log.get("time_taken", 0) for log in params["execution_time_logs"]]) or "",
        # "model_and_tool_execution_time": sum([log.get("time_taken", 0) for log in params['execution_time_logs']]) or "" + sum([log.get("time_taken", 0) for log in params['function_time_logs']]) or "",
        "execution_time_logs": params["execution_time_logs"] or {},
        "function_time_logs": params["function_time_logs"] or {},
    }


def update_usage_metrics(parsed_data, params, latency, result=None, error=None, success=False):
    """
    Update usage metrics with latency and other information.
    Handles both success and error cases with a unified interface.
    Supports both chat models and image models with proper token calculation.
    
    Args:
        parsed_data: Dictionary containing parsed request data
        params: Parameters dictionary containing execution logs
        latency: Latency metrics object
        result: Optional result dictionary from the API call (for success case)
        error: Optional error object or string (for error case)
        success: Boolean indicating if the operation was successful

    Returns:
        Updated usage dictionary
    """
    # Extract usage data from result
    usage_data = result.get('response', {}).get('usage', {}) if result else {}
    
    # Check if this is an image model (has image-specific token fields)
    is_image_model = (
        'text_input_tokens' in usage_data or 
        'image_input_tokens' in usage_data or
        'text_output_tokens' in usage_data or
        'image_output_tokens' in usage_data
    )
    
    # Calculate tokens based on model type
    if is_image_model:
        # For image models, sum up text and image tokens
        input_tokens = (
            usage_data.get('text_input_tokens', 0) + 
            usage_data.get('image_input_tokens', 0) +
            usage_data.get('cached_text_input_tokens', 0) +
            usage_data.get('cached_image_input_tokens', 0)
        )
        output_tokens = (
            usage_data.get('text_output_tokens', 0) + 
            usage_data.get('image_output_tokens', 0)
        )
        total_tokens = input_tokens + output_tokens
    else:
        # For chat models, use standard token fields
        input_tokens = usage_data.get('input_tokens', 0) or 0
        output_tokens = usage_data.get('output_tokens', 0) or 0
        total_tokens = usage_data.get('total_tokens', 0) or 0
    
    # Base fields common to both success and error cases
    update_data = {
        "service": parsed_data["service"],
        "model": parsed_data["model"],
        "orgId": parsed_data["org_id"],
        "latency": json.dumps(latency),
        "success": success,
        "apikey_object_id": params.get('apikey_object_id'),
        "expectedCost": parsed_data['tokens'].get('total_cost', 0),
        "variables": parsed_data.get('variables') or {},
        "outputTokens": output_tokens,
        "inputTokens": input_tokens,
        "total_tokens": total_tokens
    }

    # Add success-specific fields
    if success and result:
        update_data.update(
            {**(result.get("usage", {}) or {}), "prompt": parsed_data["configuration"].get("prompt") or ""}
        )

    # Add error-specific fields
    elif error and not success:
        update_data["error"] = str(error)

    # Update the usage dictionary
    parsed_data["usage"].update({**parsed_data["usage"], **update_data})

    return parsed_data["usage"]


def create_history_params(parsed_data, error=None, class_obj=None, thread_info=None):
    """
    Create history parameters for error tracking and logging.

    Args:
        parsed_data: Dictionary containing parsed request data
        error: Optional error object
        class_obj: Optional class object with aiconfig method
        thread_info: Optional thread_info dictionary containing thread_id and sub_thread_id

    Returns:
        Dictionary containing history parameters
    """
    # Use thread_info if available and parsed_data doesn't have thread_id/sub_thread_id
    thread_id = parsed_data.get("thread_id") or (thread_info.get("thread_id") if thread_info else None)
    sub_thread_id = parsed_data.get("sub_thread_id") or (thread_info.get("sub_thread_id") if thread_info else None)

    return {
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "user": parsed_data["user"],
        "message": None,
        "org_id": parsed_data["org_id"],
        "bridge_id": parsed_data["bridge_id"],
        "model": parsed_data["model"] or parsed_data["configuration"].get("model", None),
        "channel": "chat",
        "type": "error",
        "actor": "user",
        "tools_call_data": error.args[1] if error and len(error.args) > 1 else None,
        "message_id": parsed_data["message_id"],
        "AiConfig": class_obj.aiconfig() if class_obj else None,
        "firstAttemptError": parsed_data.get("firstAttemptError") or "",
        "folder_id": parsed_data.get("folder_id"),
        "folder_limit": parsed_data.get("folder_limit", 0),
        "parent_id": parsed_data.get("parent_bridge_id", ""),
        "child_id": None,
        "prompt": parsed_data["configuration"].get("prompt"),
        "plans": parsed_data.get("plans"),
        "llm_urls": [],
        "user_urls": (
            [{"url": u, "type": "image"} for u in parsed_data.get("images", [])]
            + [{"url": u, "type": "pdf"} for u in parsed_data.get("files", [])]
            + [{"url": u, "type": "audio"} for u in parsed_data.get("audios", [])]
        ),
    }


async def add_files_to_parse_data(thread_id, sub_thread_id, bridge_id):
    cache_key = f"{bridge_id}_{thread_id}_{sub_thread_id}"
    files = await find_in_cache(cache_key)
    if files:
        return json.loads(files)
    return []


async def process_background_tasks_for_playground(result, parsed_data):
    from bson import ObjectId

    from src.controllers.testcase_controller import handle_playground_testcase

    try:
        testcase_data = parsed_data.get("testcase_data", {})

        # If testcase_id exists, update in background and return immediately
        if testcase_data.get("testcase_id"):
            Flag = False

            # Update testcase in background (async task)
            async def update_testcase_background():
                try:
                    await handle_playground_testcase(result, parsed_data, Flag)
                except Exception as e:
                    logger.error(f"Error updating testcase in background: {str(e)}")

            asyncio.create_task(update_testcase_background())

        else:
            # Generate testcase_id immediately and add to response
            new_testcase_id = str(ObjectId())
            result["response"]["testcase_id"] = new_testcase_id
            parsed_data["testcase_data"]["testcase_id"] = new_testcase_id
            channel_id = f"{parsed_data.get('org_id')}_{parsed_data.get('bridge_id')}_{parsed_data.get('version_id')}"
            playground_response_format = {"type": "RTLayer", "cred": {"channel": channel_id, "ttl": 1, "apikey": Config.RTLAYER_AUTH}}
            if playground_response_format:
                await sendResponse(
                    playground_response_format,
                    parsed_data["testcase_data"],
                    success=True,
                    variables=parsed_data.get("variables", {}),
                )

            # Add the generated ID to testcase_data for the background task

            # Save testcase data in background using the same function
            async def create_testcase_background():
                try:
                    Flag = True
                    await handle_playground_testcase(result, parsed_data, Flag)
                except Exception as e:
                    logger.error(f"Error creating testcase in background: {str(e)}")

            asyncio.create_task(create_testcase_background())

    except Exception as e:
        logger.error(f"Error processing playground testcase: {str(e)}")


async def update_cost_and_last_used(parsed_data):
    try:
        await update_cost(parsed_data)
        await update_last_used(parsed_data)
    except Exception as e:
        logger.error(f"Error updating cost and last used: {str(e)}")


async def update_cost_usage_and_apikey_status_in_background(
    original_service,
    parsed_data,
    code,
    completion_success,
    fallback_service=None,
    fallback_code=None,
):
    if completion_success:
        asyncio.create_task(update_cost_and_last_used(parsed_data))

    if fallback_service:
        asyncio.create_task(mark_apikey_status_from_response(fallback_service, parsed_data, fallback_code))

    if not parsed_data.get("is_request_apikey", False):
        asyncio.create_task(mark_apikey_status_from_response(original_service, parsed_data, code))


async def sse_stream_and_finalize(class_obj, parsed_data, params, timer, thread_info, transfer_request_id, bridge_configurations, request_body=None, chat_function=None):
    import traceback as tb
    original_service = parsed_data["service"]
    original_model = parsed_data["model"]
    is_nested_stream_call = bool(
        (request_body.get("body", {}) if isinstance(request_body, dict) else {}).get("_nested_stream_call")
    )
    timer.start()
    original_error = None
    template_data = None

    try:
        from src.services.commonServices.response_caching_service import handle_response_caching
        result = await handle_response_caching(parsed_data=parsed_data, class_obj=class_obj)

    except Exception as first_err:
        original_error = str(first_err)
        logger.error(f"SSE first attempt failed ({original_service}/{original_model}): {original_error}, {tb.format_exc()}")
        fall_back = parsed_data.get("fall_back")

        if fall_back and fall_back.get("is_enable", False):
            try:
                fallback_model = fall_back.get("model", parsed_data["model"])
                fallback_service = fall_back.get("service", parsed_data["service"])
                parsed_data["model"] = fallback_model
                parsed_data["service"] = fallback_service
                parsed_data["configuration"]["model"] = fallback_model
                if fall_back.get("apikey"):
                    parsed_data["apikey"] = fall_back["apikey"]

                fb_model_config, fb_custom_config, fb_model_output_config = await load_model_configuration(
                    parsed_data["model"], parsed_data["configuration"], parsed_data["service"]
                )
                fb_custom_config = await configure_custom_settings(
                    fb_model_config["configuration"], fb_custom_config, parsed_data["service"]
                )
                fallback_params = build_service_params(
                    parsed_data,
                    fb_custom_config,
                    fb_model_output_config,
                    thread_info,
                    timer,
                    params.get("memory"),
                    bridge_configurations,
                )
                fallback_class_obj = await Helper.create_service_handler(fallback_params, parsed_data["service"])

                # Transfer the live streamer so the same SSE queue/RTLayer channel is reused
                fallback_class_obj.streamer = class_obj.streamer
                fallback_class_obj.stream_mode = True

                from src.services.commonServices.response_caching_service import handle_response_caching
                result = await handle_response_caching(parsed_data=parsed_data, class_obj=fallback_class_obj)

                if result.get("response", {}).get("data") is not None:
                    result["response"]["data"]["fallback"] = True
                    result["response"]["data"]["firstAttemptError"] = (
                        f"Original attempt failed with {original_service}/{original_model}: {original_error}. "
                        f"Retried with {parsed_data['service']}/{parsed_data['model']}"
                    )

                class_obj = fallback_class_obj
                params = fallback_params

            except Exception as retry_err:
                logger.error(f"SSE fallback attempt also failed ({parsed_data['service']}/{parsed_data['model']}): {retry_err}, {tb.format_exc()}")
                if class_obj.streamer:
                    await class_obj.streamer.emit_error(original_error, fallback_error=str(retry_err))
                    if not is_nested_stream_call:
                        await class_obj.streamer.close()
                if not parsed_data.get("is_playground"):
                    await sendResponse(
                        parsed_data.get("response_format"), str(retry_err), variables=parsed_data.get("variables", {}), meta=parsed_data.get("meta")
                    ) if (parsed_data.get("response_format") or {}).get("type") not in (None, "default") else None
                if is_nested_stream_call:
                    return {"success": False, "message": str(retry_err), "response": {}}
                return
        else:
            if class_obj.streamer:
                await class_obj.streamer.emit_error(original_error)
                if not is_nested_stream_call:
                    await class_obj.streamer.close()
            if not parsed_data.get("is_playground"):
                await sendResponse(
                    parsed_data.get("response_format"), original_error, variables=parsed_data.get("variables", {}), meta=parsed_data.get("meta")
                ) if (parsed_data.get("response_format") or {}).get("type") not in (None, "default") else None
            if is_nested_stream_call:
                return {"success": False, "message": str(original_error), "response": {}}
            return

    template_data = render_template_if_applicable(parsed_data, result)
    result.setdefault("response", {}).setdefault("data", {})
    rendered_content = result["response"]["data"].get("content", {})

    # Handle agent transfer: save agent A's metrics/history, then fire agent B with the live streamer
    transfer_agent_config = result.get("transfer_agent_config") if isinstance(result, dict) else None
    if transfer_agent_config and transfer_agent_config.get("action_type") == "transfer" and chat_function and request_body:
        try:
            result["response"]["usage"] = params["token_calculator"].get_total_usage()
            if parsed_data.get("type") != "image":
                parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
                    parsed_data["model"], parsed_data["service"]
                )
                result["response"]["usage"]["cost"] = parsed_data["tokens"].get("total_cost") or 0

            current_version_id = bridge_configurations.get(parsed_data["bridge_id"], {}).get(
                "version_id", parsed_data.get("version_id")
            )
            latency = create_latency_object(timer, params)
            update_usage_metrics(parsed_data, params, latency, result=result, success=True)

            current_history_data = {
                "bridge_id": parsed_data["bridge_id"],
                "history_params": result.get("historyParams"),
                "dataset": [parsed_data.get("usage", {})],
                "version_id": current_version_id,
                "thread_info": thread_info,
                "parent_id": parsed_data.get("parent_bridge_id", ""),
            }
            TRANSFER_HISTORY[transfer_request_id].append(current_history_data)

            target_agent_id = transfer_agent_config.get("agent_id")
            user_query = transfer_agent_config.get("user_query")

            if target_agent_id and target_agent_id in bridge_configurations:
                target_agent_cfg = bridge_configurations[target_agent_id]
                transfer_body = request_body.get("body", {}).copy()
                transfer_body.update(target_agent_cfg)
                transfer_body["bridge_id"] = target_agent_id
                transfer_body["user"] = user_query
                transfer_body["parent_bridge_id"] = parsed_data["bridge_id"]
                transfer_body["transfer_request_id"] = transfer_request_id
                transfer_body["bridge_configurations"] = bridge_configurations
                # Inject the live streamer so agent B writes to the same SSE connection
                transfer_body["_injected_streamer"] = class_obj.streamer
                if is_nested_stream_call:
                    transfer_body["_nested_stream_call"] = True
                    transfer_body["_sync_injected_stream_call"] = True

                transfer_request_body = {
                    "body": transfer_body,
                    "state": request_body.get("state", {}).copy(),
                    "path_params": request_body.get("path_params", {}),
                }
                transfer_result = await chat_function(transfer_request_body)
                if is_nested_stream_call:
                    return transfer_result
            else:
                logger.warning(f"SSE transfer: target agent {target_agent_id} not found, closing stream")
                if class_obj.streamer:
                    await class_obj.streamer.emit_error(f"Transfer target agent {target_agent_id} not found in bridge_configurations")
                    if not is_nested_stream_call:
                        await class_obj.streamer.close()
        except Exception as transfer_err:
            logger.error(f"SSE transfer handling error: {transfer_err}, {tb.format_exc()}")
            if class_obj.streamer:
                await class_obj.streamer.emit_error(str(transfer_err))
                if not is_nested_stream_call:
                    await class_obj.streamer.close()
            if is_nested_stream_call:
                return {"success": False, "message": str(transfer_err), "response": {}}
        return  # Agent B owns emit_done + close from here

    try:
        result["response"]["usage"] = params["token_calculator"].get_total_usage()
        if parsed_data.get("type") != "image":
            parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
                parsed_data["model"], parsed_data["service"]
            )
            result["response"]["usage"]["cost"] = parsed_data["tokens"].get("total_cost") or 0
        params["execution_time_logs"].append({"step": "streaming", "time_taken": round(timer.stop("streaming"), 4)})
        latency = create_latency_object(timer, params)
        if template_data and result.get("historyParams") and not parsed_data.get("is_playground"):
            result["historyParams"]["chatbot_message"] = json.dumps(rendered_content)

        if template_data and class_obj.streamer:
            await class_obj.streamer.emit_template_response(
                message_id=str(parsed_data.get("message_id") or ""),
                content=rendered_content,
                metadata=template_data,
            )

        # Update usage and run the reviewer loop BEFORE the playground/non-
        # playground split so both paths see the final, reviewed response with
        # summed tokens. Reviewer + any re-runs share the same SSE connection
        # (class_obj.streamer); emit_done is owned by this finalizer below.
        if not parsed_data["is_playground"] and result.get("response") and result["response"].get("data"):
            result["response"]["data"]["message_id"] = parsed_data["message_id"]
        update_usage_metrics(parsed_data, params, latency, result=result, success=True)
        result.setdefault("response", {}).setdefault("usage", {})
        result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

        if parsed_data.get("_reviewer_bridge_id"):
            from src.services.commonServices.reviewer_service import run_review_loop
            result, _reviewer_summary = await run_review_loop(
                parsed_data=parsed_data,
                params=params,
                timer=timer,
                thread_info=thread_info,
                bridge_configurations=bridge_configurations,
                main_result=result,
                memory=params.get("memory"),
                streamer=class_obj.streamer,
            )
            result.setdefault("response", {}).setdefault("usage", {})
            result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

        model_response = result.get("modelResponse", {}) if isinstance(result, dict) else {}
        formatted_response = result.get("response", {}) if isinstance(result, dict) else {}

        await handle_post_tool(parsed_data, result)

        if not parsed_data["is_playground"]:
            await sendResponse(
                parsed_data.get("response_format"),
                result["response"],
                success=True,
                variables=parsed_data.get("variables", {}),
                meta=parsed_data.get("meta"),
            )
            await process_background_tasks(
                parsed_data, result, params, thread_info, transfer_request_id, bridge_configurations
            )
        else:
            await process_background_tasks_for_playground(result, parsed_data)

        if class_obj.streamer:
            if getattr(class_obj, 'tool_call_limit_error', None):
                result["error"] = class_obj.tool_call_limit_error
            if result.get("error"):
                formatted_response["error"] = result["error"]
            finish_reason = (
                result.get("stream_finish_reason")
                or model_response.get("finish_reason")
                or model_response.get("status")
                or ""
            )
            if not is_nested_stream_call:
                accumulated_payload = None if template_data else formatted_response
                await class_obj.streamer.emit_done(
                    usage=formatted_response.get("usage", {}),
                    message_id=str(parsed_data.get("message_id") or ""),
                    finish_reason=finish_reason,
                    accumulated_data=accumulated_payload,
                )
                await class_obj.streamer.close()
            
        return {"success": True, "response": result.get("response", {})}
    except Exception as err:
        logger.error(f"SSE finalization error: {str(err)}, {tb.format_exc()}")
        if class_obj.streamer:
            await class_obj.streamer.emit_error(str(err))
            if not is_nested_stream_call:
                await class_obj.streamer.close()
        return {"success": False, "message": str(err), "response": {}}
