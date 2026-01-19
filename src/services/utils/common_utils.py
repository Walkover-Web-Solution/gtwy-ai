import json
import uuid
import traceback
from typing import Any, Dict, Optional
from fastapi.responses import JSONResponse
from src.services.utils.time import Timer
from src.services.commonServices.baseService.utils import axios_work
from src.services.utils.apiservice import fetch
from src.configs.serviceKeys import model_config_change
from ...controllers.conversationController import getThread
from src.services.utils.token_calculation import TokenCalculator
import src.db_services.ConfigurationServices as ConfigurationService
from .helper import Helper
from src.services.proxy.Proxyservice import get_timezone_and_org_name
from config import Config
import pydash as _
import asyncio
from datetime import datetime, timedelta, timezone
from src.services.cache_service import make_json_serializable, find_in_cache
from src.configs.model_configuration import model_config_document
from globals import *
from src.services.utils.send_error_webhook import send_error_to_webhook
from src.services.commonServices.queueService.queueLogService import sub_queue_obj
from src.services.commonServices.baseService.utils import make_request_data_and_publish_sub_queue
from src.db_services.metrics_service import create, create_orchestrator
from src.controllers.conversationController import save_sub_thread_id_and_name
from src.services.utils.ai_middleware_format import send_alert
from src.services.cache_service import find_in_cache, store_in_cache, client, REDIS_PREFIX
from src.services.utils.update_and_check_cost import update_cost,update_last_used
from ..commonServices.baseService.utils import sendResponse
from src.services.utils.rich_text_support import process_chatbot_response
from src.db_services.orchestrator_history_service import orchestrator_collector

def setup_agent_pre_tools(parsed_data, bridge_configurations):
    """
    Setup pre_tools for the current agent with its own variables.
    This function populates pre_tools args based on the agent's specific variables.
    
    Args:
        parsed_data: The parsed request data containing bridge_id, variables, and pre_tools
        bridge_configurations: Dictionary of all agent configurations
    """
    current_bridge_id = parsed_data.get('bridge_id')
    if not current_bridge_id or not bridge_configurations:
        return
    
    current_config = bridge_configurations.get(current_bridge_id, {})
    pre_tools_data = current_config.get('pre_tools_data')
    agent_variables = parsed_data.get('variables', {})
    
    if pre_tools_data and parsed_data.get('pre_tools'):
        # Get required params from pre_tools_data
        required_params = pre_tools_data.get('required_params', [])
        
        # Get variables_path mapping for the current agent
        script_id = pre_tools_data.get('script_id')
        variables_path = current_config.get('variables_path', {}).get(script_id, {}) if script_id else {}
        
        # Build args from agent's own variables
        args = {}
        for param in required_params:
            # Check if there's a mapping in variables_path for this param
            if variables_path and param in variables_path:
                # Get the mapped variable name
                mapped_variable = variables_path[param]
                # Use the mapped variable to get value from agent_variables
                if mapped_variable in agent_variables:
                    args[param] = agent_variables[mapped_variable]
            elif param in agent_variables:
                # If no mapping exists, use param directly
                args[param] = agent_variables[param]
        
        # Update the pre_tools args with agent-specific variables
        parsed_data['pre_tools']['args'] = args
        logger.info(f"Set up pre_tools for agent {current_bridge_id} with args: {args}")

async def handle_agent_transfer(result, request_body, bridge_configurations, chat_function, current_bridge_id=None, transfer_request_id=None):
    transfer_agent_config = result.get('transfer_agent_config')
    
    # Extract agent_id and user_query
    target_agent_id = transfer_agent_config.get('agent_id')
    user_query = transfer_agent_config.get('user_query')
    
    logger.info(f"Transfer detected: agent_id={target_agent_id}, user_query={user_query}")
    
    # Check if target agent exists in bridge_configurations
    if not target_agent_id or target_agent_id not in bridge_configurations:
        logger.warning(f"Transfer agent {target_agent_id} not found in bridge_configurations")
        return None
    
    # Get the target agent's configuration
    target_agent_config = bridge_configurations[target_agent_id]
    
    logger.info(f"Transferring to agent: {target_agent_config.get('name', target_agent_id)}")
    
    # Create a new request body for the transfer agent
    transfer_body = request_body.get('body', {}).copy()
    transfer_body.update(target_agent_config)
    transfer_body['bridge_id'] = target_agent_id
    transfer_body['user'] = user_query
    
    # Pass the parent_id (current bridge_id) and transfer_request_id to the next agent
    if current_bridge_id:
        transfer_body['parent_bridge_id'] = current_bridge_id
    if transfer_request_id:
        transfer_body['transfer_request_id'] = transfer_request_id
    
    # Pass complete bridge_configurations so next agent can look up version_ids
    transfer_body['bridge_configurations'] = bridge_configurations
    
    # Create a complete request structure for the transfer agent
    transfer_request_body = {
        'body': transfer_body,
        'state': request_body.get('state', {}).copy(),
        'path_params': request_body.get('path_params', {})
    }
    
    # Call chat function with the transfer agent's data
    transfer_result = await chat_function(transfer_request_body)
    
    return transfer_result


def parse_request_body(request_body):
    body = request_body.get('body', {})
    state = request_body.get('state', {})
    path_params = request_body.get('path_params', {})
    
    return {
        "body": body,
        "state": state,
        "path_params": path_params,
        "apikey": body.get("apikey"),
        "bridge_id": path_params.get('bridge_id') or body.get("bridge_id"),
        "configuration": body.get("configuration", {}),
        "thread_id": body.get("thread_id"),
        "sub_thread_id": body.get('sub_thread_id') or body.get("thread_id"),
        "org_id": state.get('profile', {}).get('org', {}).get('id', '') or body.get('org_id'),
        "user": body.get("user"),
        "tools": body.get("configuration", {}).get('tools'),
        "service": body.get("service"),
        "variables": body.get("variables") or {},
        "bridgeType": body.get('chatbot'),
        "template": body.get('template'),
        "response_format": body.get("configuration", {}).get("response_format"),
        "response_type": body.get("configuration", {}).get("response_type"),
        "model": body.get("configuration", {}).get('model'),
        "is_playground": state.get('is_playground') or body.get('is_playground') or False,
        "bridge": body.get('bridge'),
        "pre_tools": body.get('pre_tools'),
        "version": state.get('version'),
        "fine_tune_model": body.get("configuration", {}).get('fine_tune_model', {}).get('current_model', {}),
        "is_rich_text": body.get("configuration", {}).get('is_rich_text', True),
        "actions": body.get('actions', {}),
        "user_reference": body.get("user_reference", ""),
        "variables_path": body.get('variables_path') or {},
        "tool_id_and_name_mapping": body.get("tool_id_and_name_mapping"),
        "suggest": body.get('suggest', False),
        "message_id": str(uuid.uuid1()),
        "reasoning_model": body.get("configuration", {}).get('model') in {'o1-preview', 'o1-mini'},
        "gpt_memory": body.get('gpt_memory'),
        "version_id": body.get('version_id'),
        "gpt_memory_context": body.get('gpt_memory_context'),
        "usage" : {},
        "type" : body.get('configuration',{}).get('type'),
        "apikey_object_id" : body.get('apikey_object_id'),
        "audios" : [url.get('url') for url in body.get('user_urls', []) if isinstance(url, dict) and url.get('type') == 'audio' and url.get('url')],
        "images" : body.get('images') or [url.get('url') for url in body.get('user_urls', []) if isinstance(url, dict) and url.get('type') == 'image' and url.get('url')],
        "tool_call_count": body.get('tool_call_count'),
        "tokens" : {},
        "memory" : "",
        "bridge_summary" : body.get('bridge_summary'),
        "batch" : body.get('batch') or [],
        "batch_webhook" : body.get('webhook'),
        "doc_ids":body.get('ddc_ids'),
        "rag_data": body.get('rag_data'),
        "name" : body.get('name'),
        "org_name" : body.get('org_name'),
        "variables_state" : body.get('variables_state'),
        "built_in_tools" : body.get('built_in_tools') or [],
        "thread_flag" : body.get('thread_flag') or False,
        "files" : body.get('files') or [],
        "fall_back" : body.get('fall_back') or {},
        "guardrails" : body.get('bridges', {}).get('guardrails') or {},
        "testcase_data" : body.get('testcase_data') or {},
        "is_embed" : body.get('is_embed'),
        "user_id" : body.get('user_id'),
        "file_data" : body.get('video_data') or {},
        "youtube_url" : body.get('youtube_url') or None,
        "folder_id": body.get('folder_id'),
        "web_search_filters" : body.get('web_search_filters') or None,
        "parent_bridge_id": body.get('parent_bridge_id'),
        "transfer_request_id": body.get('transfer_request_id'),
        "orchestrator_flag": body.get('orchestrator_flag'),
        "batch_variables": body.get('batch_variables'),
        "chatbot_auto_answers": body.get('chatbot_auto_answers'),
        "owner_id" : state.get('profile', {}).get('owner_id') 
    }



def add_default_template(prompt):
    prompt += ' \n ### CURRENT TIME (For reference only) \n{{current_time_date_and_current_identifier}}'
    return prompt

def add_user_in_varaibles(variables, user):
    variables['_user_message'] = user
    return variables

def initialize_timer(state: Dict[str, Any]) -> Timer:
    timer_obj = Timer()
    timer_obj.defaultStart(state.get('timer', []))
    return timer_obj

async def load_model_configuration(model, configuration, service):
    model_obj = model_config_document[service][model]
    if not model_obj:
        raise BadRequestException(f"Model {model} not found in ModelsConfig.")
    
    # model_obj = modelfunc()
    model_config = model_obj['configuration']
    model_output_config = model_obj['outputConfig']
    
    custom_config = {}
    for key, config in model_config.items():
        if key == 'type' or key == 'specification':
            continue
        if "level" in config and (config["level"] == 0 or config["level"] == 1 or config["level"] == 2) or key in configuration:
            custom_config[key] = configuration.get(key, config["default"])
    
    return model_obj, custom_config, model_output_config

async def handle_fine_tune_model(parsed_data, custom_config):
    if parsed_data['configuration']['type'] == 'chat' and parsed_data['fine_tune_model'] and \
       parsed_data['model'] in {'gpt-4o-mini-2024-07-18', 'gpt-4o-2024-08-06', 'gpt-4-0613'}:
        custom_config['model'] = parsed_data['fine_tune_model']

async def handle_pre_tools(parsed_data):
    if parsed_data['pre_tools']:
        if parsed_data['pre_tools'].get('args') is None:
            parsed_data['pre_tools']['args'] = {}
        parsed_data['pre_tools']['args']['user'] = parsed_data['user']
        pre_function_response = await axios_work(
            parsed_data['pre_tools'].get('args', {}),
            {
                "url":f"https://flow.sokt.io/func/{parsed_data['pre_tools'].get('name')}"
            }
        )
        if pre_function_response.get('status') == 0:
            parsed_data['variables']['pre_function'] = f"Error while calling prefunction. Error message: {pre_function_response.get('response')}"
        else:
            parsed_data['variables']['pre_function'] = pre_function_response.get('response')

async def manage_threads(parsed_data):
    thread_id = parsed_data['thread_id']
    sub_thread_id = parsed_data['sub_thread_id']
    bridge_id = parsed_data['bridge_id']
    org_id = parsed_data['org_id']      
    
    if thread_id:
        thread_id = thread_id.strip()
        
        # Check Redis cache first for conversations
        version_id = parsed_data.get('version_id', '')
        redis_key = f"conversation_{version_id}_{thread_id}_{sub_thread_id}"
        cached_conversations = await find_in_cache(redis_key)
        
        if cached_conversations:
            # Use cached conversations from Redis
            parsed_data['configuration']["conversation"] = json.loads(cached_conversations)
            result = json.loads(cached_conversations)
            logger.info(f"Retrieved conversations from Redis cache: {redis_key}")
        else:
            # Fallback to database if not in cache
            result = await try_catch(getThread, thread_id, sub_thread_id, org_id, bridge_id)
            if result:
                parsed_data['configuration']["conversation"] = result or []
    else:
        thread_id = str(uuid.uuid1())
        sub_thread_id = thread_id
        parsed_data['thread_id'] = thread_id
        parsed_data['sub_thread_id'] = sub_thread_id
        parsed_data['gpt_memory'] = False
        result = []
    
    # cache_key = f"{bridge_id}_{thread_id}_{sub_thread_id}"
    # if len(parsed_data['files']) == 0:
    #     cached_files = await find_in_cache(cache_key)
    #     if cached_files:
    #         parsed_data['files'] = json.loads(cached_files)
    
    return {
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "result": result
    }

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
    if 'variables_state' in parsed_data and parsed_data['variables_state'] is not None:
        for var_name, var_state in parsed_data['variables_state'].items():
            if isinstance(var_state, dict) and 'status' in var_state and 'default_value' in var_state:
                # Check if variable doesn't exist, is empty/None, or if the value in variable_state is empty
                current_value = parsed_data['variables'].get(var_name)
                
                # Use default_value if:
                # 1. Variable doesn't exist in variables
                # 2. Variable exists but is None or empty string
                # 3. Variable_state has empty value
                if (current_value is None or current_value == '' or 
                    var_name not in parsed_data['variables']):
                    parsed_data['variables'][var_name] = var_state['default_value']

async def prepare_prompt(parsed_data, thread_info, model_config, custom_config):
    configuration = parsed_data['configuration']
    variables = parsed_data['variables']
    template = parsed_data['template']
    bridge_type = parsed_data['bridgeType']
    suggest = parsed_data['suggest']
    gpt_memory = parsed_data['gpt_memory']
    memory = None
    
    if configuration['type'] == 'chat' or configuration['type'] == 'reasoning':
        id = f"{thread_info['thread_id']}_{thread_info['sub_thread_id']}_{parsed_data.get('version_id') or parsed_data.get('bridge_id')}"
        parsed_data['id'] = id
        if gpt_memory:
            memory = await find_in_cache(id)
            if memory:
                # Convert bytes to string if needed
                if isinstance(memory, bytes):
                    memory = memory.decode('utf-8')
                parsed_data['memory'] = memory
            else:
                response, _ = await fetch("https://flow.sokt.io/func/scriCJLHynCG", "POST", None, None, {"threadID": id})
                parsed_data['memory'] = response
        configuration['prompt'], missing_vars = Helper.replace_variables_in_prompt(configuration['prompt'], variables)
        
        if template:
            system_prompt = template
            configuration['prompt'], missing_vars = Helper.replace_variables_in_prompt(
                system_prompt, {"system_prompt": configuration['prompt'], **variables}
            )
        
        if bridge_type and model_config.get('response_type') and suggest:
            template_content = (await ConfigurationService.get_template_by_id(Config.CHATBOT_OPTIONS_TEMPLATE_ID)).get('template', '')
            configuration['prompt'], missing_vars = Helper.replace_variables_in_prompt(
                template_content, {"system_prompt": configuration['prompt']}
            )
            custom_config['response_type'] = {"type": "json_object"}
        
        if not parsed_data['is_playground'] and bridge_type is None and model_config.get('response_type'):
            res = parsed_data['body'].get('response_type') or parsed_data['body'].get('configuration',{}).get('response_type',{"type": 'json_object'})
            match res:
                case "default":
                    custom_config['response_type'] = {"type": 'json_object'}
                case "text":
                    custom_config['response_type'] = {"type": 'text'}
                case _:
                    custom_config['response_type'] = res
        if parsed_data['bridge_summary']is not None:
            parsed_data['bridge_summary'], missing_vars = Helper.replace_variables_in_prompt(parsed_data['bridge_summary'], variables)
        
        return memory, missing_vars
    
    return memory, []

async def configure_custom_settings(model_configuration, custom_config, service):
    return await model_config_change(model_configuration, custom_config, service)

def build_service_params(parsed_data, custom_config, model_output_config, thread_info=None, timer=None, memory=None, send_error_to_webhook=None, bridge_configurations=None):
    token_calculator = TokenCalculator(parsed_data['service'], model_output_config)
    
    return {
        "customConfig": custom_config,
        "configuration": parsed_data['configuration'],
        "apikey": parsed_data['apikey'],
        "variables": parsed_data['variables'],
        "user": parsed_data['user'],
        "tools": parsed_data['tools'],
        "org_id": parsed_data['org_id'],
        "bridge_id": parsed_data['bridge_id'],
        "bridge": parsed_data['bridge'],
        "thread_id": thread_info['thread_id'] if thread_info else parsed_data['thread_id'],
        "sub_thread_id": thread_info['sub_thread_id'] if thread_info else parsed_data['sub_thread_id'],
        "model": parsed_data['model'],
        "service": parsed_data['service'],
        "modelOutputConfig": model_output_config,
        "playground": parsed_data['is_playground'],
        "template": parsed_data['template'],
        "response_format": parsed_data['response_format'],
        "execution_time_logs": [],
        "function_time_logs": [],
        "timer": timer,
        "variables_path": parsed_data['variables_path'],
        "message_id": parsed_data['message_id'],
        "bridgeType": parsed_data['bridgeType'],
        "tool_id_and_name_mapping": parsed_data["tool_id_and_name_mapping"],
        "reasoning_model": parsed_data['reasoning_model'],
        "memory": memory,
        "type": parsed_data['configuration'].get('type'),
        "token_calculator": token_calculator,
        "apikey_object_id" : parsed_data['apikey_object_id'],
        "images" : parsed_data['images'],
        "audios" : parsed_data.get('audios'),
        "tool_call_count": parsed_data['tool_call_count'],
        "rag_data": parsed_data['rag_data'],
        "name" : parsed_data['name'],
        "org_name" : parsed_data['org_name'],
        "send_error_to_webhook": send_error_to_webhook,
        "built_in_tools" : parsed_data['built_in_tools'],
        "files" : parsed_data['files'],
        "file_data" : parsed_data['file_data'],
        "youtube_url" : parsed_data['youtube_url'],
        "web_search_filters" : parsed_data['web_search_filters'],
        "folder_id": parsed_data.get('folder_id'),
        "bridge_configurations": bridge_configurations,
        "owner_id" : parsed_data.get('owner_id')

    }

async def process_background_tasks(parsed_data, result, params, thread_info, transfer_request_id=None, bridge_configurations=None):

    """
    Process background tasks for saving history and publishing to queue.
    Handles both regular flow and transfer chain scenarios.
    Also handles orchestrator mode where multiple agents are saved in a single entry.
    """
    # Check if orchestrator_flag is enabled (from body or parsed_data)
    orchestrator_flag = parsed_data.get('orchestrator_flag') or parsed_data.get('body', {}).get('orchestrator_flag')
    
    # Check if this is part of a transfer chain
    is_transfer_chain = transfer_request_id and transfer_request_id in TRANSFER_HISTORY and len(TRANSFER_HISTORY[transfer_request_id]) > 0
    
    if is_transfer_chain:
        # This is the final agent in a transfer chain
        # Get the correct version_id from bridge_configurations for the final agent
        bridge_configs = bridge_configurations or {}
        final_version_id = bridge_configs.get(parsed_data['bridge_id'], {}).get('version_id', parsed_data['version_id'])
        
        # Add current agent's history
        current_history_data = {
            'bridge_id': parsed_data['bridge_id'],
            'history_params': result.get('historyParams'),
            'dataset': [parsed_data['usage']],
            'version_id': final_version_id,
            'thread_info': thread_info,
            'parent_id': parsed_data.get('parent_bridge_id', '')
        }
        TRANSFER_HISTORY[transfer_request_id].append(current_history_data)
        
        # Save all transfer history (each agent in the chain)
        transfer_chain = TRANSFER_HISTORY[transfer_request_id]
        
        # If orchestrator_flag is true, save all agents in a single orchestrator entry
        if orchestrator_flag:
            # Update history_params with prompts from bridge_configurations
            for idx, history_entry in enumerate(transfer_chain):
                if history_entry['history_params']:
                    agent_bridge_id = history_entry['bridge_id']
                    if bridge_configs and agent_bridge_id in bridge_configs:
                        agent_config = bridge_configs[agent_bridge_id].get('configuration', {})
                        history_entry['history_params']['prompt'] = agent_config.get('prompt')
            
            # Save all agents in a single orchestrator entry
            asyncio.create_task(create_orchestrator(transfer_chain, thread_info))
        else:
            # Regular transfer chain - save each agent separately
            for idx, history_entry in enumerate(transfer_chain):
                # Update parent_id and child_id in history_params based on chain position
                if history_entry['history_params']:
                    # Set parent_id from the previous entry's bridge_id
                    history_entry['history_params']['parent_id'] = history_entry.get('parent_id', '')
                    
                    # Set child_id from the next entry's bridge_id (None if last in chain)
                    if idx < len(transfer_chain) - 1:
                        history_entry['history_params']['child_id'] = transfer_chain[idx + 1]['bridge_id']
                    else:
                        history_entry['history_params']['child_id'] = None
                    
                    # Add prompt from bridge_configurations if available
                    agent_bridge_id = history_entry['bridge_id']
                    if bridge_configs and agent_bridge_id in bridge_configs:
                        agent_config = bridge_configs[agent_bridge_id].get('configuration', {})
                        history_entry['history_params']['prompt'] = agent_config.get('prompt')
                
                # Save history to database
                asyncio.create_task(create(
                    history_entry['dataset'],
                    history_entry['history_params'],
                    history_entry['version_id'],
                    history_entry['thread_info']
                ))
        
        # Clean up transfer history
        del TRANSFER_HISTORY[transfer_request_id]
    else:
        # Regular flow (no transfer or first agent that didn't transfer)
        # Always set parent_id and child_id in history_params for consistency
        if result.get('historyParams'):
            result['historyParams']['parent_id'] = parsed_data.get('parent_bridge_id', '')
            result['historyParams']['child_id'] = None
        
        # Save single history entry
        asyncio.create_task(create(
            [parsed_data['usage']], 
            result["historyParams"], 
            parsed_data['version_id'], 
            thread_info
        ))
    
    # Publish to queue (for both transfer and non-transfer cases)
    data = await make_request_data_and_publish_sub_queue(parsed_data, result, params, thread_info)
    data = make_json_serializable(data)
    await sub_queue_obj.publish_message(data)


async def process_background_tasks_for_error(parsed_data, error):
    # Combine the tasks into a single asyncio.gather call
    tasks = [
        send_alert(data={"org_name" : parsed_data['org_name'], "bridge_name" : parsed_data['name'], "configuration": parsed_data['configuration'], "error": str(error), "message_id": parsed_data['message_id'], "bridge_id": parsed_data['bridge_id'], "message": "Exception for the code", "org_id": parsed_data['org_id']}),
        create([parsed_data['usage']],parsed_data['historyParams'] , parsed_data['version_id']),
        save_sub_thread_id_and_name(parsed_data['thread_id'], parsed_data['sub_thread_id'], parsed_data['org_id'], parsed_data['thread_flag'], parsed_data['response_format'], parsed_data['bridge_id'], parsed_data['user'])
    ]
    # Filter out None values
    await asyncio.gather(*[task for task in tasks if task is not None], return_exceptions=True)

def build_service_params_for_batch(parsed_data, custom_config, model_output_config):
    
    return {
        "customConfig": custom_config,
        "configuration": parsed_data['configuration'],
        "apikey": parsed_data['apikey'],
        "variables": parsed_data['variables'],
        "user": parsed_data['user'],
        "tools": parsed_data['tools'],
        "org_id": parsed_data['org_id'],
        "bridge_id": parsed_data['bridge_id'],
        "bridge": parsed_data['bridge'],
        "model": parsed_data['model'],
        "service": parsed_data['service'],
        "modelOutputConfig": model_output_config,
        "playground": parsed_data['is_playground'],
        "template": parsed_data['template'],
        "response_format": parsed_data['response_format'],
        "execution_time_logs": [],
        "variables_path": parsed_data['variables_path'],
        "message_id": parsed_data['message_id'],
        "bridgeType": parsed_data['bridgeType'],
        "reasoning_model": parsed_data['reasoning_model'],
        "type": parsed_data['configuration'].get('type'),
        "apikey_object_id" : parsed_data['apikey_object_id'],
        "batch" : parsed_data['batch'],
        "webhook" : parsed_data['batch_webhook'],
        "folder_id": parsed_data.get('folder_id'),
        "batch_variables": parsed_data['batch_variables'],
        "processed_prompts": parsed_data.get('processed_prompts', [])
    }


async def updateVariablesWithTimeZone(variables, org_id):
    org_name = ''
    async def getTimezoneOfOrg():
        data = await get_timezone_and_org_name(org_id)
        timezone = data.get('timezone') or "+5:30"
        hour, minutes = timezone.split(':')
        return int(hour), int(minutes), data.get('name') or "", (data.get('meta') or {}).get('identifier', '')
    hour, minutes, org_name, identifier = await getTimezoneOfOrg()
    if 'timezone' in variables and variables['timezone']:
        hour, minutes = Helper.get_current_time_with_timezone(variables['timezone'])
        identifier = variables['timezone']
    current_time = datetime.now(timezone.utc)
    current_time = current_time + timedelta(hours=hour, minutes=minutes)
    if identifier == '' and 'timezone' not in variables:
        identifier = 'Asia/Calcutta'
    variables['current_time_date_and_current_identifier'] = current_time.strftime("%Y-%m-%d") + ' ' + current_time.strftime("%H:%M:%S") + ' ' + current_time.strftime("%A") + ' (' + identifier + ')'
    return variables, org_name


def filter_missing_vars(missing_vars, variables_state):
            # Handle if variables_state is None
            if variables_state is None:
                return missing_vars
            
            # Iterate through keys in missing_vars
            keys_to_remove = [key for key, value in variables_state.items() if value != 'required']
            
            # Remove the keys from missing_vars that are in the keys_to_remove list
            for key in keys_to_remove:
                if key in missing_vars:
                    del missing_vars[key]
            
            return missing_vars

def get_service_by_model(model): 
    return next((s for s in model_config_document if model in model_config_document[s]), None)

def send_error(bridge_id, org_id, error_message, error_type, bridge_name=None, is_embed=None, user_id=None, thread_id=None, service=None):
    asyncio.create_task(send_error_to_webhook(
        bridge_id, org_id, error_message, error_type=error_type, bridge_name=bridge_name, is_embed=is_embed, user_id=user_id, thread_id=thread_id, service=service
    ))

def restructure_json_schema(response_type, service):
    match service:
        case 'openai':
            schema = response_type.get('json_schema', {})
            del response_type['json_schema']
            for key, value in schema.items():
                response_type[key] = value
            return response_type
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
    if not configuration or 'response_type' not in configuration:
        return True, None
        
    response_type = configuration.get('response_type')
    if not response_type:
        return True, None
        
    # If response_type is a string (like "default"), allow it
    if isinstance(response_type, str):
        return True, None
        
    # Check if type is json_schema
    if response_type.get('type') != 'json_schema':
        return True, None
        
    # If json_schema key exists and is None, return error
    if 'json_schema' in response_type and response_type['json_schema'] is None:
        return False, "json_schema should be a valid JSON, not None"
        
    # If json_schema key exists and is not None, validate it's valid JSON
    if 'json_schema' in response_type and response_type['json_schema'] is not None:
        try:
            # If it's already a dict/object, it's valid
            if isinstance(response_type['json_schema'], dict):
                return True, None
            # If it's a string, try to parse it as JSON
            elif isinstance(response_type['json_schema'], str):
                json.loads(response_type['json_schema'])
                return True, None
            else:
                return False, "json_schema should be a valid JSON object or string"
        except (json.JSONDecodeError, TypeError):
            return False, "json_schema should be a valid JSON"
            
    # If json_schema key is not present, it's valid (allowed case)
    return True, None


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
        "model_execution_time": sum([log.get("time_taken", 0) for log in params['execution_time_logs']]) or "",
        # "model_and_tool_execution_time": sum([log.get("time_taken", 0) for log in params['execution_time_logs']]) or "" + sum([log.get("time_taken", 0) for log in params['function_time_logs']]) or "",
        "execution_time_logs": params['execution_time_logs'] or {},
        "function_time_logs": params['function_time_logs'] or {}
    }


def update_usage_metrics(parsed_data, params, latency, result=None, error=None, success=False):
    """
      be metrics with latency and other information.
    Handles both success and error cases with a unified interface.
    
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
    # Base fields common to both success and error cases
    update_data = {
        "service": parsed_data['service'],
        "model": parsed_data['model'],
        "orgId": parsed_data['org_id'],
        "latency": json.dumps(latency),
        "success": success,
        "apikey_object_id": params.get('apikey_object_id'),
        "expectedCost": parsed_data['tokens'].get('total_cost', 0),
        "variables": parsed_data.get('variables') or {},
        "outputTokens": result.get('response', {}).get('usage', {}).get('output_tokens', 0) or 0 if result else 0,
        "inputTokens": result.get('response', {}).get('usage', {}).get('input_tokens', 0) or 0 if result else 0,
        "total_tokens": result.get('response', {}).get('usage', {}).get('total_tokens', 0) or 0 if result else 0
    }
    
    # Add success-specific fields
    if success and result:
        update_data.update({
            **(result.get("usage", {}) or {}),
            "prompt": parsed_data['configuration'].get("prompt") or ""
        })
    
    # Add error-specific fields
    elif error and not success:
        update_data["error"] = str(error)
    
    # Update the usage dictionary
    parsed_data['usage'].update({
        **parsed_data['usage'],
        **update_data
    })
    
    return parsed_data['usage']


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
    thread_id = parsed_data.get('thread_id') or (thread_info.get('thread_id') if thread_info else None)
    sub_thread_id = parsed_data.get('sub_thread_id') or (thread_info.get('sub_thread_id') if thread_info else None)
    
    return {
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "user": parsed_data['user'],
        "message": None,
        "org_id": parsed_data['org_id'],
        "bridge_id": parsed_data['bridge_id'],
        "model": parsed_data['model'] or parsed_data['configuration'].get("model", None),
        "channel": 'chat',
        "type": "error",
        "actor": "user",
        'tools_call_data': error.args[1] if error and len(error.args) > 1 else None,
        "message_id": parsed_data['message_id'],
        "AiConfig": class_obj.aiconfig() if class_obj else None,
        "folder_id": parsed_data.get('folder_id'),
        "folder_limit": parsed_data.get('folder_limit', 0),
        "parent_id": parsed_data.get('parent_bridge_id', ''),
        "child_id": None,
        "prompt": parsed_data['configuration'].get('prompt'),
        "llm_urls": [],
        "user_urls": ([{"url": u, "type": "image"} for u in parsed_data.get("images", [])] + [{"url": u, "type": "pdf"} for u in parsed_data.get("files", [])] + [{"url": u, "type": "audio"} for u in parsed_data.get("audios", [])])
    }


async def add_files_to_parse_data(thread_id, sub_thread_id, bridge_id):
    cache_key = f"{bridge_id}_{thread_id}_{sub_thread_id}"
    files = await find_in_cache(cache_key)
    if files:
        return json.loads(files)
    return []

async def process_background_tasks_for_playground(result, parsed_data):
    from src.controllers.testcase_controller import handle_playground_testcase
    from bson import ObjectId
    
    try:
        testcase_data = parsed_data.get('testcase_data', {})
        
        # If testcase_id exists, update in background and return immediately
        if testcase_data.get('testcase_id'):
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
            result['response']['testcase_id'] = new_testcase_id
            parsed_data['testcase_data']['testcase_id'] = new_testcase_id
            await sendResponse(parsed_data['body']['bridge_configurations']['playground_response_format'], parsed_data['testcase_data'], success=True, variables=parsed_data.get('variables',{}))
            
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

async def update_cost_and_last_used_in_background(parsed_data):
    """Kick off the async cost cache update using the data available on parsed_data."""
    if not isinstance(parsed_data, dict):
        logger.warning("Skipping background cost update due to invalid parsed data.")
        return

    asyncio.create_task(update_cost_and_last_used(parsed_data))

    
