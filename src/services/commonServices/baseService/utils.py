import pydash as _
import re
import httpx
import asyncio
import json 
from src.configs.constant import service_name
import pydash as _
from src.services.utils.apiservice import fetch
from fastapi import Request
import datetime
from src.controllers.rag_controller import get_text_from_vectorsQuery
from globals import *
from src.db_services.ConfigurationServices import get_bridges_without_tools, update_bridge
from src.services.utils.ai_call_util import call_gtwy_agent
from src.services.utils.built_in_tools.firecrawl import call_firecrawl_scrape
from globals import *
from src.services.cache_service import store_in_cache, find_in_cache, client, REDIS_PREFIX
from src.configs.constant import redis_keys,inbuild_tools

def clean_json(data):
    """Recursively remove keys with empty string, empty list, or empty dictionary."""
    if isinstance(data, dict):
        return {k: clean_json(v) for k, v in data.items() if v not in ['', []]}
    elif isinstance(data, list):
        return [clean_json(item) for item in data]
    else:
        return data

def validate_tool_call(service, response):
    match service: # TODO: Fix validation process.
        case  'openai_completion' | 'groq' | 'grok' | 'open_router' | 'mistral' | 'gemini'| 'ai_ml':
            tool_calls = response.get('choices', [])[0].get('message', {}).get("tool_calls", [])
            return len(tool_calls) > 0 if tool_calls is not None else False
        case 'openai':
            return any(output.get('type') == 'function_call' for output in response.get('output', []))
        case 'anthropic':
            for item in response.get('content', []):
                if item.get('name') == 'JSON_Schema_Response_Format':
                    response['content'][0]['text'] = json.dumps(item.get('input'))
                    return False
            return response.get('stop_reason') == 'tool_use'
        case _:
            return False

async def axios_work(data, function_payload):
    try:    
        response,rs_headers = await fetch(function_payload.get("url"),"POST",function_payload.get("headers",{}),None,data) # required is not send then it will still hit the curl
        return {
            'response': response,
            'metadata':{
                'flowHitId': rs_headers.get('flowHitId'),
                "type": "function"
            },
            'status': 1
        }
        
    except Exception as err:
        logger.error("Error calling function axios_work => ",function_payload.get("url"),  err)
        return {
            'response': str(err),
            'metadata':{
                "type": "function"
            },
            'status': 0
        }

# [?] won't work for the case addess.name[0]
def get_nested_value(dictionary, key_path):
    keys = key_path.split('.')
    for key in keys:
        if isinstance(dictionary, dict) and key in dictionary:
            dictionary = dictionary[key]
        else:
            return None
    return dictionary

# https://docs.google.com/document/d/1WkXnaeAhTUdAfo62SQL0WASoLw-wB9RD9N-IeUXw49Y/edit?tab=t.0 => to see the variables example
def transform_required_params_to_required(properties, variables={}, variables_path={}, function_name=None, parent_key=None, parentValue=None):
    if not isinstance(properties, dict):
        return properties
    transformed_properties = properties.copy()
    if properties.get('type') == 'array' or properties.get('type') == 'object':
        return {'items':properties}
    for key, value in properties.items():
        if value.get('required_params') is not None:
            transformed_properties[key]['required'] = value.pop('required_params')
        key_to_find = f"{parent_key}.{key}" if parent_key else key
        if variables_path is not None and variables_path.get(function_name) and key_to_find in variables_path[function_name]:
            variable_path_value = variables_path[function_name][key_to_find]
            if_variable_has_value = get_nested_value(variables, variable_path_value)
            if if_variable_has_value:
                del transformed_properties[key]
                if parentValue and 'required' in parentValue and key in parentValue['required']:
                    parentValue['required'].remove(key)
                continue
        for prop_key in ['parameter', 'properties']:
            if prop_key in value:
                transformed_properties[key]['properties'] = transform_required_params_to_required(value.pop(prop_key), variables, variables_path, function_name, key, value)
                break
        else:
            items = value.get('items', {})
            item_type = items.get('type')
            if item_type == 'object':
                nextedObject = {'properties': transform_required_params_to_required( items.get('properties', {}), variables, variables_path, function_name, key, value)}
                nextedObject = {**nextedObject, "required": items.get('required', []), "type": item_type}
                transformed_properties[key]['items'] = nextedObject
            elif item_type == 'array':
                transformed_properties[key]['items'] = transform_required_params_to_required( items.get('items', {}), variables, variables_path, function_name, key, value)
                transformed_properties[key]['items']['type'] = 'array'
    return transformed_properties

def tool_call_formatter(configuration: dict, service: str, variables: dict, variables_path: dict) -> dict: # changes
    if service == service_name['openai_completion'] or service == service_name['open_router'] or service == service_name['mistral'] or service == service_name['gemini'] or service == service_name['ai_ml']:
        data_to_send =  [
            {
                'type': 'function',
                'function': {
                    'name': transformed_tool['name'],
                    # "strict": True,
                    'description': transformed_tool['description'],
                    'parameters': {
                        'type': 'object',
                        'properties': clean_json(transform_required_params_to_required(transformed_tool.get('properties', {}), variables=variables, variables_path=variables_path, function_name=transformed_tool['name'], parentValue={'required': transformed_tool.get('required', [])})),
                        'required': transformed_tool.get('required'),
                        # "additionalProperties": False,
                    }
                }
            } for transformed_tool in configuration.get('tools', [])
        ]
        return data_to_send
    elif service == service_name['openai']:
        data_to_send =  [
            {
                'type': 'function',
                'name': transformed_tool['name'],
                # "strict": True,
                'description': transformed_tool['description'],
                'parameters': {
                    'type': 'object',
                    'properties': clean_json(transform_required_params_to_required(transformed_tool.get('properties', {}), variables=variables, variables_path=variables_path, function_name=transformed_tool['name'], parentValue={'required': transformed_tool.get('required', [])})),
                    'required': transformed_tool.get('required'),
                    # "additionalProperties": False,
                }
            } for transformed_tool in configuration.get('tools', [])
        ]
        return data_to_send
    elif service == service_name['anthropic']:
        return [
            transformed_tool if transformed_tool['name'] == 'JSON_Schema_Response_Format' else {
                'name': transformed_tool['name'],
                'description': transformed_tool['description'],
                'input_schema': {
                    "type": "object",
                    'properties': clean_json(transform_required_params_to_required(transformed_tool.get('properties', {}), variables=variables, variables_path=variables_path, function_name=transformed_tool['name'], parentValue={'required': transformed_tool.get('required', [])})),
                    'required': transformed_tool.get('required')
                }
            } for transformed_tool in configuration.get('tools', [])
        ]
    elif service == service_name['groq'] or service == service_name['grok']:
        return [
            {
            "type": "function",
            "function": {
                "name": transformed_tool['name'],
                "description": transformed_tool['description'],
                "parameters": {
                    "type": "object",
                    "properties": clean_json(transform_required_params_to_required(transformed_tool.get('properties', {}), variables=variables, variables_path=variables_path, function_name=transformed_tool['name'], parentValue={'required': transformed_tool.get('required', [])})),
                    "required": transformed_tool.get('required'),
                },
                    },
            } for transformed_tool in configuration.get('tools', [])
        ]

async def send_request(url, data, method, headers):
    try:
        if isinstance(headers, str):
            headers = json.loads(headers)
        return await fetch(url,method,headers,None, data)
    except Exception as e:
        logger.error(f'Unexpected error:, {url}, {str(e)}')
        return {'error': 'Unexpected error', 'details': str(e)}
    
async def send_message(cred, data ):
    try:
        response = await fetch(
            f"https://api.rtlayer.com/message?apiKey={cred['apikey']}","POST",None,None,
            {
                **cred,
                'message': json.dumps(data)
            }
        )
        return response
    except httpx.RequestError as error:
        logger.error(f'send message error=>, {str(error)}')
    except Exception as e:
        logger.error(f'Unexpected send message error=>, {str(e)}')


async def sendResponse(response_format, data, success = False, variables={}):
    data_to_send = {
        'response' if success else 'error': data,
        'success': success
    }
    match response_format['type']:
        case 'RTLayer' : 
            return await send_message(cred = response_format['cred'], data=data_to_send)
        case 'webhook':
            data_to_send['variables'] = variables
            return await send_request(**response_format['cred'], method='POST', data=data_to_send)

async def process_data_and_run_tools(codes_mapping, self):
    try: 
        self.timer.start()
        executed_functions = []
        responses = []
        tool_call_logs = {**codes_mapping} 

        # Prepare tasks for async execution
        tasks = []
        for tool_call_key, tool in codes_mapping.items():
            name = tool['name']

            # Get corresponding function code mapping
            tool_mapping = {} if self.tool_id_and_name_mapping[name] else {"error": True, "response": "Wrong Function name"}
            tool_data = {**tool, **tool_mapping}

            if not tool_data.get("response"):
                # if function is present in db/NO response, create task for async processing
                if self.tool_id_and_name_mapping[name].get('type') == 'RAG':
                    # Get the resource_to_collection_mapping from tool_id_and_name_mapping
                    resource_to_collection_mapping = self.tool_id_and_name_mapping[name].get('resource_to_collection_mapping', {})
                    task = get_text_from_vectorsQuery(
                        {**tool_data.get("args"), "org_id": self.org_id}, 
                        Flag=True, 
                        owner_id=self.owner_id,
                        resource_to_collection_mapping=resource_to_collection_mapping
                    ) 
                elif self.tool_id_and_name_mapping[name].get('type') == 'AGENT':
                    agent_args = {
                        "org_id": self.org_id, 
                        "bridge_id": self.tool_id_and_name_mapping[name].get("bridge_id"), 
                        "user": tool_data.get("args").get("_query"), 
                        "variables": {key: value for key, value in tool_data.get("args").items() if key != "user"}
                    }
                    
                    # Add thread_id and sub_thread_id if bridge requires it
                    if self.tool_id_and_name_mapping[name].get('requires_thread_id', False):
                        agent_args["thread_id"] = self.thread_id
                        agent_args["sub_thread_id"] = self.sub_thread_id
                    if self.tool_id_and_name_mapping[name].get('version_id', False):
                        agent_args["version_id"] = self.tool_id_and_name_mapping[name].get('version_id')
                    
                    # Pass timer state to maintain latency tracking in recursive calls
                    if hasattr(self, 'timer') and hasattr(self.timer, 'getTime'):
                        agent_args["timer_state"] = self.timer.getTime()
                    
                    # Pass bridge_configurations if available
                    if hasattr(self, 'bridge_configurations') and self.bridge_configurations:
                        agent_args["bridge_configurations"] = self.bridge_configurations
                    
                    task = call_gtwy_agent(agent_args)
                elif self.tool_id_and_name_mapping[name].get('type') == inbuild_tools["Gtwy_Web_Search"]:
                    task = call_firecrawl_scrape(tool_data.get("args"))
                else: 
                    task = axios_work(tool_data.get("args"), self.tool_id_and_name_mapping[name])
                tasks.append((tool_call_key, tool_data, task))
                executed_functions.append(name)
            else:
                # If function is not present in db/response exists, append to responses
                responses.append({
                    'tool_call_id': tool_call_key,
                    'role': 'tool',
                    'name': tool['name'],
                    'content': json.dumps(tool_data['response'])
                })
                # Update tool_call_logs with existing response
                tool_call_logs[tool_call_key] = {**tool, "response": tool_data['response']}

        # Execute all tasks concurrently if any exist
        if tasks:
            task_results = await asyncio.gather(
                *[task[2] for task in tasks], return_exceptions=True
            ) # return_exceptions use for the handle the error occurs from the task

            # Process each result
            for i, (tool_call_key, tool_data, _) in enumerate(tasks):
                result = task_results[i]

                # Handle any exceptions or errors
                if isinstance(result, Exception):
                    response = {"error": "Error during async task execution", "details": str(result)}
                elif tool_data.get('error'):
                    response = {"error":"Args / Input is not proper JSON"}
                else:
                    response = result.get("response","") if result.get("status") == 1 else {"error": result.get("response","")}

                # Append formatted response
                responses.append({
                    'tool_call_id': tool_call_key,  # Replacing with tool_call_key
                    'role': 'tool',
                    'name': tool_data['name'],
                    'content': json.dumps(response)
                })

                # Update tool_call_logs with the response
                tool_call_logs[tool_call_key] = {**tool_data, "data" : result or response, "id": self.tool_id_and_name_mapping[tool_data['name']].get("name")}
        # Create mapping by tool_call_id (now tool_call_key) for return
        mapping = {resp['tool_call_id']: resp for resp in responses}

        # Record executed function names and timing
        executed_names = ", ".join(executed_functions) if executed_functions else "No functions executed"
        self.function_time_logs.append({"step": executed_names, "time_taken": self.timer.stop("process_data_and_run_tools")})

        return responses, mapping, tool_call_logs

    except Exception as error:
        print(f"Error in process_data_and_run_tools: {error}")
        traceback.print_exc()
        raise error
  


def make_code_mapping_by_service(responses, service):
    codes_mapping = {}
    function_list = []
    match service:
        case 'openai_completion' | 'groq' | 'grok' | 'open_router' | 'mistral' | 'gemini' | 'ai_ml':

            for tool_call in responses['choices'][0]['message']['tool_calls']:
                name = tool_call['function']['name']
                error = False
                try:
                    args = json.loads(tool_call['function']['arguments'])
                except json.JSONDecodeError:
                    args = {
                        "error": tool_call['function']['arguments']
                    }
                    error = True
                codes_mapping[tool_call["id"]] = {
                    'name': name,
                    'args': args,
                    "error": error
                }
                function_list.append(name)
        case 'openai':

            for tool_call in [output for output in responses['output'] if output.get('type') == 'function_call']:
                name = tool_call['name']
                error = False
                try:
                    args = json.loads(tool_call['arguments'])
                except json.JSONDecodeError:
                    args = {
                        "error": tool_call['arguments']
                    }
                    error = True
                codes_mapping[tool_call["id"]] = {
                    'name': name,
                    'args': args,
                    "error": error
                }
                function_list.append(name)
        case 'anthropic':
            for tool_call in [item for item in responses['content'] if item['type'] == 'tool_use']:  # Skip the first item
                name = tool_call['name']
                args = tool_call['input']
                codes_mapping[tool_call["id"]] = {
                    'name': name,
                    'args': args,
                    "error": False
                }
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
    
    attributes = ['is_playground', 'version', 'profile']
    for attr in attributes:
        if hasattr(request.state, attr):
            state_data[attr] = getattr(request.state, attr)
    
    if hasattr(request.state, 'timer'):
        state_data['timer'] = request.state.timer
        
    if hasattr(request, 'path_params'):
        path_params = request.path_params
        
    body = convert_datetime(body)
    state_data = convert_datetime(state_data)
        
    result = {
        'body': body,
        'state': state_data,
        'path_params': path_params
    }
    return result

async def make_request_data_and_publish_sub_queue(parsed_data, result, params, thread_info=None):
    suggestion_content = {'data': {'content': {}}}
    suggestion_content['data']['content'] = result.get('historyParams', {}).get('message')
    
    # Extract user and assistant messages for Hippocampus
    user_message = parsed_data.get('user', '')
    assistant_message = result.get('historyParams', {}).get('message', '')
    
    data = {
        "save_sub_thread_id_and_name" : {
            "org_id" : parsed_data.get('org_id'),
            "thread_id" : thread_info.get('thread_id') if thread_info else parsed_data.get('thread_id'),
            "sub_thread_id" : thread_info.get('sub_thread_id') if thread_info else parsed_data.get('sub_thread_id'),
            "thread_flag" : parsed_data.get('thread_flag'),
            "response_format" : parsed_data.get('response_format'),
            "bridge_id" : parsed_data.get('bridge_id'),
            "user" : parsed_data.get('user')
        },
        "metrics_service": {
            "dataset": [parsed_data.get('usage', {})],
            "history_params": result.get("historyParams", {}),
            "version_id": parsed_data.get('version_id')
        },
        "validateResponse": {
            "alert_flag": parsed_data.get('alert_flag'),
            "configration": parsed_data.get('configuration'),
            "bridgeId": parsed_data.get('bridge_id'),
            "message_id": parsed_data.get('message_id'),
            "org_id": parsed_data.get('org_id')
        },
        "total_token_calculation": {
            "tokens": parsed_data.get('tokens', {}),
            "bridge_id": parsed_data.get('bridge_id')
        },
        "chatbot_suggestions" : {
            "response_format": parsed_data.get('response_format'),
            "assistant": suggestion_content,
            "user": parsed_data.get('user'),
            "bridge_summary": parsed_data.get('bridge_summary'),
            "thread_id": parsed_data.get('thread_id'),
            "sub_thread_id": parsed_data.get('sub_thread_id'),
            "configuration": params.get('configuration', {}),
            "org_id" : parsed_data.get('org_id')
        },
        "handle_gpt_memory" : {
            "id" : parsed_data.get('id'),
            "user" : parsed_data.get('user'),
            "assistant" : result.get('modelResponse'),
            "purpose" : parsed_data.get('memory'),
            "gpt_memory_context" : parsed_data.get('gpt_memory_context'),
            "org_id" : parsed_data.get('org_id')
        },
        "check_handle_gpt_memory" : {
            "gpt_memory" : parsed_data.get('gpt_memory'),
            "type" : parsed_data.get('configuration', {}).get('type')
        },
        "check_chatbot_suggestions" : {
            "bridgeType" : parsed_data.get('bridgeType'),
        },
        "save_to_hippocampus" : {
            "user_message" : user_message,
            "assistant_message" : assistant_message,
            "bridge_id" : parsed_data.get('bridge_id'),
            "bridge_name" : parsed_data.get('name', ''),
            "chatbot_auto_answers": parsed_data.get('chatbot_auto_answers')
        },
        "type" : parsed_data.get('type'),
        "save_files_to_redis" : {
            "thread_id" : parsed_data.get('thread_id'),
            "sub_thread_id" : parsed_data.get('sub_thread_id'),
            "bridge_id" : parsed_data.get('bridge_id'),
            "files" : parsed_data.get('files')
        }
    }

    return data


def makeFunctionName(name):
    return re.sub(r'[^a-zA-Z0-9_-]', '', name)

async def total_token_calculation(tokens, bridge_id):
    total_tokens = tokens.get('inputTokens', 0) + tokens.get('outputTokens', 0)
    bridge_data = (await get_bridges_without_tools(bridge_id, org_id=None)).get("bridges")
    if bridge_data and 'total_tokens' in bridge_data:
        total_tokens = bridge_data['total_tokens'] + total_tokens
    else:
        total_tokens = total_tokens
    
    # Fix: update_bridge expects update_fields as a dictionary parameter
    await update_bridge(bridge_id=bridge_id, update_fields={'total_tokens': total_tokens})

async def save_files_to_redis(thread_id, sub_thread_id, bridge_id, files):
    cache_key = f"{redis_keys['files_']}{bridge_id}_{thread_id}_{sub_thread_id}"
    existing_cache = await find_in_cache(cache_key)
    if existing_cache:
        try:
            cached_files = json.loads(existing_cache)
            if cached_files == files:
                await client.expire(f"{REDIS_PREFIX}{cache_key}", 604800)
            else:
                await store_in_cache(cache_key, files, 604800)
        except (json.JSONDecodeError, Exception):
            await store_in_cache(cache_key, files, 604800)
    else:
        await store_in_cache(cache_key, files, 604800)
