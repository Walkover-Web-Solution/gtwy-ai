import asyncio
import logging

import src.db_services.ConfigurationServices as ConfigurationService
from models.mongo_connection import db
from src.services.utils.common_utils import updateVariablesWithTimeZone

from .getConfiguration_utils import (
    add_connected_agents,
    add_rag_tool,
    add_web_crawling_tool,
    get_bridge_data,
    setup_api_key,
    setup_configuration,
    setup_tool_choice,
    setup_tools,
    validate_bridge,
)
from .helper import Helper
from .update_and_check_cost import check_bridge_api_folder_limits

apiCallModel = db["apicalls"]

logger = logging.getLogger(__name__)


def _normalize_apikeys(apikeys_dict, key_type="API"):
    normalized_keys = {
        key_type: {},
        "status": {}
    }

    try:
        for service_name, apikey_data in apikeys_dict.items():
            if isinstance(apikey_data, dict) and "apikey" in apikey_data:
                normalized_keys[key_type][service_name] = apikey_data["apikey"]
                normalized_keys["status"][service_name] = apikey_data.get("status", "working")
            else:
                logger.warning(f"{key_type} key not found for service: {service_name}")
    except (KeyError, TypeError) as e:
        logger.error(f"Error accessing {key_type} keys: {e}")

    return normalized_keys


async def _prepare_configuration_response(
    configuration,
    service,
    bridge_id,
    apikey,
    template_id=None,
    variables=None,
    org_id="",
    variables_path=None,
    version_id=None,
    extra_tools=None,
    built_in_tools=None,
    guardrails=None,
    web_search_filters=None,
    orchestrator_flag=None,
    chatbot=False,
    override_fields={}
):
    """Internal helper to build configuration response for a single bridge."""

    variables = variables or {}
    extra_tools = extra_tools or []
    built_in_tools = built_in_tools or []
    web_search_filters = web_search_filters or {}

    # Fetch bridge data
    agent_data, resolved_bridge_id = await get_bridge_data(bridge_id, org_id, version_id)
    bridges = agent_data.get("bridges", {})
    chatbot = bridges.get("bridgeType") == "chatbot"

    # Validate bridge existence and status before any limit checks
    validation_result = await validate_bridge(agent_data)
    if validation_result:
        return validation_result, None, None, resolved_bridge_id

    # Limit checks
    limit_error = await check_bridge_api_folder_limits(agent_data, version_id)
    if limit_error:
        return limit_error, None, None, resolved_bridge_id

    # Setup configuration and service
    configuration, service = setup_configuration(configuration, bridges, service)

    if service == "openai_response":
        service = "openai"
    if bridges.get("openai_completion"):
        service = "openai_completion"

    service = service.lower() if service else ""

    # Initialize apikey_status with default value
    apikey_status = {}

    # Normalize API keys — reads apikeys_combined (pre-normalization full dict with limits)
    apikeys_dict = bridges.get("apikeys_combined", {})
    if apikeys_dict:
        apiandstatus = _normalize_apikeys(apikeys_dict, "apikeys")
        bridges["apikeys"] = apiandstatus.get("apikeys")
        apikey_status = apiandstatus.get("status", {})

    # Normalize folder API keys
    folder_apikeys_dict = bridges.get("folder_apikeys", {})
    if folder_apikeys_dict:
        apiandstatus = _normalize_apikeys(folder_apikeys_dict, "folder_apikeys")
        bridges["folder_apikeys"] = apiandstatus.get("folder_apikeys")
        apikey_status = apiandstatus.get("status", {})

    # Capture raw dicts before normalization for limit response data
    apikey_src = apikeys_dict or folder_apikeys_dict or {}
    is_request_apikey = bool(apikey)
    apikey = setup_api_key(service, bridges, apikey, chatbot)
    apikey_object_id = bridges.get("apikey_object_id")

    # Overriding fields from Body (if Given)
    auto_model_select = override_fields.get("auto_model_select") or bridges.get("auto_model_select")
    chatbot_auto_answers = override_fields.get("chatbot_auto_answers") or bridges.get("chatbot_auto_answers")
    cache_on = override_fields.get("cache_on") or bridges.get("cache_on")

    service_apikeys = {}
    merged_apikeys = {
        **(bridges.get("apikeys", {}) or {}),
        **(bridges.get("folder_apikeys", {}) or {}),
    }

    for service_name, encrypted_key in merged_apikeys.items():
        if not encrypted_key:
            continue
        try:
            service_apikeys[service_name] = Helper.decrypt(encrypted_key)
        except Exception:
            continue

    if service and apikey:
        service_apikeys[service] = apikey

    # Handle image type early return
    if configuration["type"] == "image":
        image_config = {
            "configuration": configuration,
            "service": service,
            "apikey": apikey,
            "is_request_apikey": is_request_apikey,
            "apikey_object_id": apikey_object_id,
            "RTLayer": False,
            "settings": bridges.get("settings", {}),
            "bridge_id": bridges.get("parent_id", bridges.get("_id")),
            "version_id": version_id or bridges.get("published_version_id"),
        }
        return None, image_config, agent_data, resolved_bridge_id

    # Tool choice
    configuration["tool_choice"] = setup_tool_choice(configuration, bridges, service)

    variables_path_bridge = bridges.get("variables_path", {})

    tools, tool_id_and_name_mapping, variables_path_bridge = setup_tools(bridges, variables_path_bridge, extra_tools)
    configuration.pop("tools", None)
    configuration["tools"] = tools

    RTLayer = True if configuration and "RTLayer" in configuration else False

    template_content = await ConfigurationService.get_template_by_id(template_id) if template_id else None

    # Pre-tools — build list for later processing in chat_multiple_agents
    pre_tools_data_for_later = []
    raw_pre_tools = bridges.get("pre_tools", [])

    if raw_pre_tools:
        for tool_entry in raw_pre_tools:
            if not isinstance(tool_entry, dict):
                continue
            tool_type = tool_entry.get("type")
            tool_config = tool_entry.get("config", {})
            tool_args = tool_entry.get("args", {})
            pre_tools_data_for_later.append({
                "_type": tool_type,
                "config": tool_config,
                "args": tool_args,
            })
    raw_post_tool = bridges.get("folder_post_tool") or {}
    raw_post_tool_script_id = raw_post_tool.get('script_id', {}) if raw_post_tool else {}
    post_tool_data = None
    if raw_post_tool:
        variables_path_post_tool = bridges.get("variables_path", {}).get(raw_post_tool_script_id, {})
        post_tool_data = {
            **raw_post_tool,
            "args": variables_path_post_tool,
            "config": raw_post_tool.get("config", {}),
        }

    rag_data = bridges.get("doc_ids")
    gpt_memory_context = bridges.get("gpt_memory_context")
    gpt_memory = bridges.get("gpt_memory")

    tone = bridges.get("settings", {}).get("tone", {})
    responseStyle = bridges.get("settings", {}).get("responseStyle", {})
    configuration["prompt"] = Helper.append_tone_and_response_style_prompts(
        configuration["prompt"], tone, responseStyle
    )

    add_rag_tool(tools, tool_id_and_name_mapping, rag_data)

    gtwy_web_search_filters = web_search_filters or bridges.get("gtwy_web_search_filters") or {}
    add_web_crawling_tool(
        tools,
        tool_id_and_name_mapping,
        built_in_tools or bridges.get("built_in_tools"),
        gtwy_web_search_filters,
    )
    if rag_data:
        configuration["prompt"] = Helper.add_doc_description_to_prompt(configuration["prompt"], rag_data)

    variables, org_name = await updateVariablesWithTimeZone(variables, org_id)

    add_connected_agents(bridges, tools, tool_id_and_name_mapping, orchestrator_flag)
    web_search_filters_value = web_search_filters or bridges.get("web_search_filters") or {}

    base_config = {
        "configuration": configuration,
        "pre_tools_data": pre_tools_data_for_later,
        "post_tool_data": post_tool_data,
        "service": service,
        "apikey": apikey,
        "auto_model_select": auto_model_select,
        "service_apikeys": service_apikeys,
        "is_request_apikey": is_request_apikey,
        "apikey_object_id": apikey_object_id,
        "apikey_status": apikey_status,
        "RTLayer": RTLayer,
        "template": template_content.get("template") if template_content else None,
        "user_reference": bridges.get("user_reference", ""),
        "variables_path": variables_path or variables_path_bridge,
        "tool_id_and_name_mapping": tool_id_and_name_mapping,
        "gpt_memory": gpt_memory,
        "version_id": version_id or bridges.get("published_version_id"),
        "gpt_memory_context": gpt_memory_context,
        "settings": bridges.get("settings", {}),
        "variables": variables,
        "rag_data": rag_data,
        "actions": bridges.get("actions", []),
        "name": bridges.get("name") or "",
        "org_name": org_name,
        "bridge_id": bridges.get("parent_id", bridges.get("_id")),
        "variables_state": bridges.get("agent_info", {}).get("variables_state", {}),
        "built_in_tools": built_in_tools or bridges.get("built_in_tools"),
        "is_embed": bridges.get("folder_type") == "embed",
        "user_id": bridges.get("user_id"),
        "folder_id": bridges.get("folder_id"),
        "wrapper_id": bridges.get("wrapper_id"),
        "web_search_filters": web_search_filters_value,
        "chatbot_auto_answers": chatbot_auto_answers,
        "cache_on": cache_on,
        "richui_templates": bridges.get("richui_templates"),
        "meta": bridges.get("meta"),
        "reviewer_agent": str(bridges.get("settings", {}).get("reviewer_agent") or ""),
        "api_collection": apikey_src,
        "limit": {
            "bridge": {
                "limit": bridges.get("bridge_limit"),
                "limit_start_date": bridges.get("bridge_limit_start_date"),
                "limit_reset_period": bridges.get("bridge_limit_reset_period"),
            },
            "folder": {
                "limit": bridges.get("folder_limit"),
                "limit_start_date": bridges.get("folder_limit_start_date"),
                "limit_reset_period": bridges.get("folder_limit_reset_period"),
            },
            "apikey": {
                "limit": apikey_src.get(service, {}).get("apikey_limit"),
                "limit_start_date": apikey_src.get(service, {}).get("apikey_limit_start_date"),
                "limit_reset_period": apikey_src.get(service, {}).get("apikey_limit_reset_period"),
            },
        },
    }

    return None, base_config, agent_data, resolved_bridge_id


async def _collect_connected_agent_configs(agent_data, org_id, visited):
    """Recursively collect configurations for connected agents."""

    if not agent_data:
        return {}

    bridge_payload = agent_data.get("bridges", {})
    connected_agents = bridge_payload.get("connected_agents", {})
    connected_agent_details = bridge_payload.get("connected_agent_details", {})

    # Build pending list; pre-populate visited to prevent duplicate concurrent fetches
    pending = []
    for _, agent_info in connected_agents.items():
        bridge_id_value = agent_info.get("bridge_id")
        if not bridge_id_value or bridge_id_value in visited:
            continue
        visited.add(bridge_id_value)
        agent_details = connected_agent_details.get(bridge_id_value) or {}
        merged_info = {**agent_details, **agent_info}
        pending.append((bridge_id_value, merged_info))

    aggregated_configs = {}

    if pending:
        async def _fetch_agent(bridge_id_value, merged_info):
            version_id_value = merged_info.get("version_id")
            configuration_override = (
                merged_info.get("configuration") if isinstance(merged_info.get("configuration"), dict) else None
            )
            service_override = merged_info.get("service")
            apikey_override = merged_info.get("apikey")
            template_id_override = merged_info.get("template_id")
            variables_override = merged_info.get("variables") if isinstance(merged_info.get("variables"), dict) else {}
            variables_path_override = merged_info.get("variables_path")
            extra_tools_override = (
                merged_info.get("extra_tools") if isinstance(merged_info.get("extra_tools"), list) else []
            )
            built_in_tools_override = (
                merged_info.get("built_in_tools") if isinstance(merged_info.get("built_in_tools"), list) else []
            )
            web_search_filters_override = (
                merged_info.get("web_search_filters") if isinstance(merged_info.get("web_search_filters"), dict) else {}
            )
            guardrails_override = merged_info["guardrails"] if "guardrails" in merged_info else None

            try:
                error, child_config, child_agent_data, resolved_child_id = await _prepare_configuration_response(
                    configuration_override,
                    service_override,
                    bridge_id_value,
                    apikey_override,
                    template_id_override,
                    variables_override,
                    org_id,
                    variables_path_override,
                    version_id_value,
                    extra_tools_override,
                    built_in_tools_override,
                    guardrails_override,
                    web_search_filters_override,
                )
            except Exception as exc:
                logger.error(f"Error fetching configuration for connected agent {bridge_id_value}: {exc}")
                return bridge_id_value, None, None, None

            if error:
                logger.error(f"Skipping connected agent {bridge_id_value} due to error response: {error}")
                return bridge_id_value, None, None, None

            return bridge_id_value, child_config, child_agent_data, resolved_child_id

        # Fetch all sibling agents concurrently
        results = await asyncio.gather(*[_fetch_agent(bid, info) for bid, info in pending])

        for bridge_id_value, child_config, child_agent_data, resolved_child_id in results:
            if child_config is None:
                continue

            key = bridge_id_value or resolved_child_id
            resolved_id = resolved_child_id or bridge_id_value

            if resolved_id:
                child_config["bridge_id"] = resolved_id
                visited.add(resolved_id)

            aggregated_configs[key] = child_config

            nested = await _collect_connected_agent_configs(child_agent_data, org_id, visited)
            aggregated_configs.update(nested)

    reviewer_bridge_id_raw = bridge_payload.get("settings", {}).get("reviewer_agent")
    reviewer_bridge_id = str(reviewer_bridge_id_raw) if reviewer_bridge_id_raw else ""
    if reviewer_bridge_id and reviewer_bridge_id not in visited:
        try:
            error, reviewer_config, reviewer_agent_data, resolved_reviewer_id = await _prepare_configuration_response(
                None, None, reviewer_bridge_id, None,
                None, None, org_id, None, None,
                None, None, None, None,
            )
        except Exception as exc:
            logger.error(f"Error fetching configuration for reviewer agent {reviewer_bridge_id}: {exc}")
        else:
            if error:
                logger.error(f"Skipping reviewer agent {reviewer_bridge_id} due to error response: {error}")
            else:
                resolved_id = resolved_reviewer_id or reviewer_bridge_id
                if resolved_id:
                    reviewer_config["bridge_id"] = resolved_id
                    visited.add(resolved_id)
                visited.add(reviewer_bridge_id)
                aggregated_configs[reviewer_bridge_id] = reviewer_config

                nested = await _collect_connected_agent_configs(reviewer_agent_data, org_id, visited)
                aggregated_configs.update(nested)

    return aggregated_configs


async def getConfiguration(
    configuration,
    service,
    bridge_id,
    apikey,
    template_id=None,
    variables=None,
    org_id="",
    variables_path=None,
    version_id=None,
    extra_tools=None,
    built_in_tools=None,
    guardrails=None,
    web_search_filters=None,
    orchestrator_flag=None,
    chatbot=False,
    override_fields={}
):
    """
    Get configuration for a bridge with all necessary tools and settings.
    """

    error, base_config, agent_data, resolved_bridge_id = await _prepare_configuration_response(
        configuration,
        service,
        bridge_id,
        apikey,
        template_id,
        variables,
        org_id,
        variables_path,
        version_id,
        extra_tools,
        built_in_tools,
        guardrails,
        web_search_filters,
        orchestrator_flag,
        chatbot,
        override_fields
    )

    if error:
        return error

    config_key = resolved_bridge_id or base_config.get("bridge_id") or bridge_id

    if base_config.get("bridge_id") != config_key and config_key:
        base_config["bridge_id"] = config_key

    visited = set()
    for identifier in (bridge_id, resolved_bridge_id, config_key):
        if identifier:
            visited.add(identifier)

    connected_configs = await _collect_connected_agent_configs(agent_data, org_id, visited)

    bridge_configurations = {}
    if config_key:
        bridge_configurations[config_key] = base_config

    bridge_configurations.update(connected_configs)

    response_payload = {"success": True, "bridge_configurations": bridge_configurations}

    if config_key:
        response_payload["primary_bridge_id"] = config_key

    return response_payload
