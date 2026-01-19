import logging
import src.db_services.ConfigurationServices as ConfigurationService
from .helper import Helper
from models.mongo_connection import db
from src.services.utils.common_utils import updateVariablesWithTimeZone
from .getConfiguration_utils import (
    validate_bridge, get_bridge_data, setup_configuration, setup_tool_choice,
    setup_tools, setup_api_key, setup_pre_tools, add_rag_tool,
    add_anthropic_json_schema, add_connected_agents, add_web_crawling_tool
)
from .update_and_check_cost import check_bridge_api_folder_limits

apiCallModel = db['apicalls']
from globals import *

logger = logging.getLogger(__name__)

def _normalize_apikeys(apikeys_dict, key_type="API"):
    normalized_keys = {}
    
    try:
        for service_name, apikey_data in apikeys_dict.items():
            if isinstance(apikey_data, dict) and 'apikey' in apikey_data:
                normalized_keys[service_name] = apikey_data['apikey']
            else:
                logger.warning(f"{key_type} key not found for service: {service_name}")
    except (KeyError, TypeError) as e:
        logger.error(f"Error accessing {key_type} keys: {e}")
    
    return normalized_keys

async def _prepare_configuration_response(configuration, service, bridge_id, apikey, template_id=None,
                                          variables=None, org_id="", variables_path=None, version_id=None,
                                          extra_tools=None, built_in_tools=None, guardrails=None,
                                          web_search_filters=None, orchestrator_flag=None, chatbot=False):
    """Internal helper to build configuration response for a single bridge."""

    variables = variables or {}
    extra_tools = extra_tools or []
    built_in_tools = built_in_tools or []
    web_search_filters = web_search_filters or {}

    # Fetch bridge data
    result, bridge_data, resolved_bridge_id = await get_bridge_data(bridge_id, org_id, version_id)
    chatbot = True if bridge_data.get('bridges', {}).get('bridgeType') == 'chatbot' else False
    if not chatbot:
        chatbot = True if bridge_data.get('bridgeType') == 'chatbot' else False

    # Limit checks
    limit_error = await check_bridge_api_folder_limits(result.get('bridges'), bridge_data, version_id)
    if limit_error:
        return limit_error, None, None, resolved_bridge_id

    # Validate bridge
    validation_result = await validate_bridge(bridge_data, result)
    if validation_result:
        return validation_result, None, None, resolved_bridge_id

    # Setup configuration and service
    configuration, service = setup_configuration(configuration, result, service)

    if service == 'openai_response':
        service = 'openai'
    if result.get('bridges', {}).get('openai_completion'):
        service = 'openai_completion'

    service = service.lower() if service else ""

    # Normalize API keys
    apikeys_dict = result.get('bridges', {}).get('apikeys', {})
    if apikeys_dict:
        result['bridges']['apikeys'] = _normalize_apikeys(apikeys_dict, "API")

    # Normalize folder API keys
    folder_apikeys_dict = result.get('bridges', {}).get('folder_apikeys', {})
    if folder_apikeys_dict:
        result['bridges']['folder_apikeys'] = _normalize_apikeys(folder_apikeys_dict, "Folder API")

    apikey = setup_api_key(service, result, apikey, chatbot)
    apikey_object_id = result.get('bridges', {}).get('apikey_object_id')

    # Handle image type early return
    if configuration['type'] == 'image':
        image_config = {
            'configuration': configuration,
            'service': service,
            'apikey': apikey,
            'apikey_object_id': apikey_object_id,
            'RTLayer': False,
            'bridge_id': result['bridges'].get('parent_id', result['bridges'].get('_id')),
            'version_id': version_id or result.get('bridges', {}).get('published_version_id'),
        }
        return None, image_config, result, resolved_bridge_id

    # Tool choice
    configuration['tool_choice'] = setup_tool_choice(configuration, result, service)

    bridge = result.get('bridges')
    variables_path_bridge = bridge.get('variables_path', {})

    tools, tool_id_and_name_mapping, variables_path_bridge = setup_tools(result, variables_path_bridge, extra_tools)
    configuration.pop('tools', None)
    configuration['tools'] = tools

    RTLayer = True if configuration and 'RTLayer' in configuration else False

    template_content = await ConfigurationService.get_template_by_id(template_id) if template_id else None

    # Pre-tools will be set up later in chat_multiple_agents with agent-specific variables
    pre_tools_name, pre_tools_args = None, None
    # Store pre_tools_data for later processing
    pre_tools_data_for_later = None
    if bridge.get('pre_tools'):
        api_data = result.get('bridges', {}).get('pre_tools_data', [{}])[0]
        del api_data['_id']
        if(api_data.get('bridge_ids')):
            del api_data['bridge_ids']
        if(api_data.get('folder_id')):
            del api_data['folder_id']
        if api_data:
            pre_tools_name = api_data.get('script_id')
            pre_tools_data_for_later = api_data

    rag_data = bridge.get('doc_ids')
    gpt_memory_context = bridge.get('gpt_memory_context')
    gpt_memory = result.get('bridges', {}).get('gpt_memory')

    tone = configuration.get('tone', {})
    responseStyle = configuration.get('responseStyle', {})
    configuration['prompt'] = Helper.append_tone_and_response_style_prompts(
        configuration['prompt'], tone, responseStyle
    )

    add_rag_tool(tools, tool_id_and_name_mapping, rag_data)
    
    gtwy_web_search_filters = web_search_filters or result.get('bridges', {}).get('gtwy_web_search_filters') or {}
    add_web_crawling_tool(tools, tool_id_and_name_mapping, built_in_tools or result.get('bridges', {}).get('built_in_tools'), gtwy_web_search_filters)
    add_anthropic_json_schema(service, configuration, tools)

    if rag_data:
        configuration['prompt'] = Helper.add_doc_description_to_prompt(configuration['prompt'], rag_data)

    variables, org_name = await updateVariablesWithTimeZone(variables, org_id)

    add_connected_agents(result, tools, tool_id_and_name_mapping, orchestrator_flag)

    guardrails_value = guardrails if guardrails is not None else (result.get('bridges', {}).get('guardrails') or {})
    web_search_filters_value = web_search_filters or result.get('bridges', {}).get('web_search_filters') or {}

    base_config = {
        'configuration': configuration,
        'pre_tools': {'name': pre_tools_name, 'args': pre_tools_args} if pre_tools_name else None,
        'pre_tools_data': pre_tools_data_for_later,  # Store pre_tools_data for later processing
        'service': service,
        'apikey': apikey,
        'apikey_object_id': apikey_object_id,
        'RTLayer': RTLayer,
        'template': template_content.get('template') if template_content else None,
        'user_reference': result.get('bridges', {}).get('user_reference', ''),
        'variables_path': variables_path or variables_path_bridge,
        'tool_id_and_name_mapping': tool_id_and_name_mapping,
        'gpt_memory': gpt_memory,
        'version_id': version_id or result.get('bridges', {}).get('published_version_id'),
        'gpt_memory_context': gpt_memory_context,
        'tool_call_count': result.get('bridges', {}).get('tool_call_count', 3),
        'variables': variables,
        'rag_data': rag_data,
        'actions': result.get('bridges', {}).get('actions', []),
        'name': bridge_data.get('name') or bridge_data.get('bridges', {}).get('name') or '',
        'org_name': org_name,
        'bridge_id': result['bridges'].get('parent_id', result['bridges'].get('_id')),
        'variables_state': result.get('bridges', {}).get('variables_state', {}),
        'built_in_tools': built_in_tools or result.get('bridges', {}).get('built_in_tools'),
        'fall_back': result.get('bridges', {}).get('fall_back') or {},
        'guardrails': guardrails_value,
        "is_embed": result.get('bridges', {}).get("folder_type") == 'embed',
        "user_id": result.get("bridges", {}).get("user_id"),
        'folder_id': result.get('bridges', {}).get('folder_id'),
        'web_search_filters': web_search_filters_value,
        'chatbot_auto_answers': bridge_data.get('bridges', {}).get('chatbot_auto_answers')
    }

    return None, base_config, result, resolved_bridge_id


async def _collect_connected_agent_configs(result, org_id, visited):
    """Recursively collect configurations for connected agents."""

    if not result:
        return {}

    bridge_payload = result.get('bridges', {})
    connected_agents = bridge_payload.get('connected_agents', {})
    connected_agent_details = bridge_payload.get('connected_agent_details', {})

    aggregated_configs = {}

    for _, agent_info in connected_agents.items():
        bridge_id_value = agent_info.get('bridge_id')
        if not bridge_id_value or bridge_id_value in visited:
            continue

        agent_details = connected_agent_details.get(bridge_id_value) or {}
        merged_info = {**agent_details, **agent_info}

        version_id_value = merged_info.get('version_id')
        configuration_override = merged_info.get('configuration') if isinstance(merged_info.get('configuration'), dict) else None
        service_override = merged_info.get('service')
        apikey_override = merged_info.get('apikey')
        template_id_override = merged_info.get('template_id')
        variables_override = merged_info.get('variables') if isinstance(merged_info.get('variables'), dict) else {}
        variables_path_override = merged_info.get('variables_path')
        extra_tools_override = merged_info.get('extra_tools') if isinstance(merged_info.get('extra_tools'), list) else []
        built_in_tools_override = merged_info.get('built_in_tools') if isinstance(merged_info.get('built_in_tools'), list) else []
        web_search_filters_override = merged_info.get('web_search_filters') if isinstance(merged_info.get('web_search_filters'), dict) else {}
        guardrails_override = merged_info['guardrails'] if 'guardrails' in merged_info else None

        try:
            error, child_config, child_result, resolved_child_id = await _prepare_configuration_response(
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
                web_search_filters_override
            )
        except Exception as exc:
            logger.error(f"Error fetching configuration for connected agent {bridge_id_value}: {exc}")
            continue

        if error:
            logger.error(f"Skipping connected agent {bridge_id_value} due to error response: {error}")
            continue

        key = bridge_id_value or resolved_child_id
        resolved_id = resolved_child_id or bridge_id_value

        if resolved_id:
            child_config['bridge_id'] = resolved_id
            visited.add(resolved_id)
        if bridge_id_value:
            visited.add(bridge_id_value)

        aggregated_configs[key] = child_config

        nested = await _collect_connected_agent_configs(child_result, org_id, visited)
        aggregated_configs.update(nested)

    return aggregated_configs

async def getConfiguration(configuration, service, bridge_id, apikey, template_id=None, variables={}, 
                           org_id="", variables_path=None, version_id=None, extra_tools=[], built_in_tools=[], guardrails={}, web_search_filters={}, orchestrator_flag=None, chatbot=False):
    """
    Get configuration for a bridge with all necessary tools and settings.
    """

    error, base_config, result, resolved_bridge_id = await _prepare_configuration_response(
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
        chatbot
    )

    if error:
        return error

    config_key = resolved_bridge_id or base_config.get('bridge_id') or bridge_id

    if base_config.get('bridge_id') != config_key and config_key:
        base_config['bridge_id'] = config_key

    visited = set()
    for identifier in (bridge_id, resolved_bridge_id, config_key):
        if identifier:
            visited.add(identifier)

    connected_configs = await _collect_connected_agent_configs(result, org_id, visited)

    bridge_configurations = {}
    if config_key:
        bridge_configurations[config_key] = base_config

    bridge_configurations.update(connected_configs)

    response_payload = {'success': True, 'bridge_configurations': bridge_configurations}

    if config_key:
        response_payload['primary_bridge_id'] = config_key

    return response_payload
