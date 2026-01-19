import src.db_services.ConfigurationServices as ConfigurationService
from src.services.utils.helper import Helper
from models.mongo_connection import db
from src.services.commonServices.baseService.utils import makeFunctionName
from src.services.utils.service_config_utils import tool_choice_function_name_formatter
from config import Config
from src.configs.constant import inbuild_tools

apiCallModel = db['apicalls']
from globals import *

async def validate_bridge(bridge_data, result):
    """Validate bridge status and existence"""
    bridge_status = bridge_data.get('bridges', {}).get('bridge_status') or bridge_data.get('bridge_status',0)
    if bridge_status == 0:
        raise Exception("Bridge is Currently Paused")
    
    if not result.get('success'):
        return {
            'success': False,
            'error': "bridge_id does not exist"
        }
    return None

async def get_bridge_data(bridge_id, org_id, version_id):
    """Fetch bridge data from database"""
    result = await ConfigurationService.get_bridges_with_tools_and_apikeys(
        bridge_id=bridge_id, 
        org_id=org_id, 
        version_id=version_id
    )
    
    bridge_id = bridge_id or result.get('bridges', {}).get('parent_id')
    
    if version_id:
        bridge_data = await ConfigurationService.get_bridges_with_redis(
            bridge_id=bridge_id, 
            org_id=org_id
        )
    else:
        bridge_data = result
        
    return result, bridge_data, bridge_id

def setup_configuration(configuration, result, service):
    """Setup and merge configuration from database and input"""
    db_configuration = result.get('bridges', {}).get('configuration', {})
    service = service or (result.get('bridges', {}).get('service', '').lower())
    
    if configuration:
        db_configuration.update(configuration)
    
    return db_configuration, service

def setup_tool_choice(configuration, result, service):
    """Setup tool choice configuration"""
    tool_choice_ids = configuration.get('tool_choice', [])
    toolchoice = None
    
    # Find tool choice from API calls
    for _, api_data in result.get('bridges', {}).get('apiCalls', {}).items():
        if api_data['_id'] in tool_choice_ids:
            toolchoice = api_data.get('title') or makeFunctionName(api_data['endpoint_name'] or api_data['function_name'])
            break
    if not toolchoice:
        connected_agents = result.get('bridges',{}).get('connected_agents',{})
        for agent_name, agent_data in connected_agents.items():
            if tool_choice_ids == agent_data['bridge_id']:
                toolchoice = makeFunctionName(agent_name)
                break
    
    # Find choice type
    found_choice = None
    for choice in ['auto', 'none', 'required', 'default', 'any']:
        if choice in tool_choice_ids:
            found_choice = choice
            break
    
    return tool_choice_function_name_formatter(
        service=service, 
        configuration=configuration, 
        toolchoice=toolchoice, 
        found_choice=found_choice
    )

def process_api_call_tool(api_data, variables_path_bridge):
    """Process a single API call and convert it to a tool format"""
    name_of_function = api_data.get('title')  or makeFunctionName(api_data['endpoint_name'] or api_data['function_name'])
    
    # Skip if status is paused and no function name
    if api_data.get('status') == 0 and not name_of_function:
        return None, None
    
    # Setup tool mapping
    tool_mapping = {
        "url": f"https://flow.sokt.io/func/{api_data.get('script_id')}",
        "headers": {},
        "name": api_data.get('script_id')
    }
    
    # Process variables filled by gateway
    variables_fill_by_gtwy = list(variables_path_bridge.get(api_data.get("script_id"), {}).keys())
    
    properties = api_data.get("fields", {})
    
    # Remove properties that are filled by gateway
    for key in variables_fill_by_gtwy:
        properties.pop(key, None)
    
    # Filter required parameters
    required = api_data.get("required_params", [])
    required = [key for key in required if key not in variables_fill_by_gtwy]
    
    # Create tool format
    tool_format = {
        "type": "function",
        "name": name_of_function,
        "description": api_data.get('description'),
        "properties": properties,
        "required": required
    }
    
    return tool_format, tool_mapping

def process_extra_tool(tool):
    """Process an extra tool and convert it to tool format"""
    if not isinstance(tool, dict) or not tool.get("url"):
        return None, None, {}

    tool_name = tool.get('name')
    if not tool_name:
        return None, None, {}

    properties = tool.get('fields', {}) or {}
    if not isinstance(properties, dict):
        properties = {}

    required_params = tool.get("required_params", []) or []
    if not isinstance(required_params, list):
        required_params = []

    tool_format = {
        "type": "function",
        "name": makeFunctionName(tool_name),
        "description": tool.get('description'),
        "properties": properties,
        "required": required_params
    }

    tool_mapping = {
        "url": tool.get("url"),
        "headers": tool.get("headers", {}),
        "name": tool_name
    }
    variable_path = tool.get('tool_and_variable_path', {}) or {}
    # Remove properties that are filled by gateway
    for key in variable_path:
        properties.pop(key, None)

    return tool_format, tool_mapping, {tool_name: variable_path}

def setup_tools(result, variables_path_bridge, extra_tools):
    """Setup tools and tool mappings"""
    tools = []
    tool_id_and_name_mapping = {}
    variable_path ={}
    # Process API calls
    for _, api_data in result.get('bridges', {}).get('apiCalls', {}).items():
        tool_format, tool_mapping = process_api_call_tool(api_data, variables_path_bridge)
        if tool_format:
            name_of_function = tool_format["name"]
            tools.append(tool_format)
            tool_id_and_name_mapping[name_of_function] = tool_mapping
    # Process extra tools
    for tool in extra_tools:
        tool_format, tool_mapping, path = process_extra_tool(tool)
        variable_path.update(path)
        if tool_format:
            name_of_function = tool_format["name"]
            tools.append(tool_format)
            tool_id_and_name_mapping[name_of_function] = tool_mapping
    return tools, tool_id_and_name_mapping, {**variables_path_bridge, **variable_path}

def setup_api_key(service, result, apikey, chatbot):
    """Setup API key for the service"""
    db_apikeys = result.get('bridges', {}).get('apikeys', {})
    db_apikeys_object_id = result.get('bridges', {}).get('apikey_object_id', {})
    # Get API key for the service
    db_api_key = db_apikeys.get(service)

    if service == 'ai_ml' and not apikey and not db_api_key:
        apikey = Config.AI_ML_APIKEY
    if service == 'openai_completion':
        db_api_key = db_apikeys.get('openai')
    
    # Check for folder API keys if folder_id exists
    folder_api_key = result.get('bridges', {}).get('folder_apikeys', {}).get(service)
    if folder_api_key:
        db_api_key = folder_api_key

    # Validate API key existence
    if chatbot and (service == 'openai'):
        model = result.get('bridges', {}).get('configuration', {}).get('model')
        # If both keys are not present
        if not (apikey or db_api_key):
            # Use Config.OPENAI_API_KEY only if model is gpt-5-nano
            if model == 'gpt-5-nano':
                apikey = Config.OPENAI_API_KEY_GPT_5_NANO
            else:
                raise Exception('Could not find api key or Agent is not Published')
    
    if not (apikey or db_api_key):
        raise Exception('Could not find api key or Agent is not Published')
    
    # Handle fallback configuration
    fallback_config = result.get('bridges', {}).get('fall_back')
    if fallback_config:
        fallback_service = fallback_config.get('service')
        fallback_apikey = db_apikeys.get(fallback_service)
        if fallback_apikey:
            result['bridges']['fall_back']['apikey'] = Helper.decrypt(fallback_apikey)
            result['bridges']['fall_back']['apikey_object_id'] = db_apikeys_object_id.get(fallback_service)
    
    # Use provided API key or decrypt from database
    return apikey if apikey else Helper.decrypt(db_api_key)

def setup_pre_tools(bridge, result, variables):
    """Setup pre-tools configuration"""
    pre_tools = bridge.get('pre_tools', [])
    if not pre_tools:
        return None, None
    
    api_data = result.get('bridges', {}).get('pre_tools_data', [{}])[0]
    if api_data is None:
        raise Exception("Didn't find the pre_function")
    
    name = api_data.get('title')  or makeFunctionName(api_data['endpoint_name'] or api_data['function_name'])
    required_params = api_data.get('required_params', [])
    
    args = {}
    for param in required_params:
        if param in variables:
            args[param] = variables[param]
    
    return name, args

def add_rag_tool(tools, tool_id_and_name_mapping, rag_data):
    """Add RAG tool if RAG data is available"""
    if not rag_data or rag_data == []:
        return
    
    # Create mapping of resource_id to collection_id
    resource_to_collection_mapping = {}
    for data in rag_data:
        if isinstance(data, dict):
            resource_id = data.get('resource_id', '')
            collection_id = data.get('collection_id', '')
            if resource_id and collection_id:
                resource_to_collection_mapping[resource_id] = collection_id
    
    tools.append({
        'type': 'function', 
        'name': 'get_knowledge_base_data', 
        'description': "When user want to take any data from the knowledge, Call this function to get the corresponding resource id", 
        'properties': {
            "resource_id": {
                "description": "send resource id",
                "type": "string",
                "enum": [],
                "required_params": [],
                "parameter": {}
            },
            "query": {
                "description": "query to ask from the knowledge base",
                "type": "string",
                "enum": [],
                "required_params": [],
                "parameter": {}
            }
        }, 
        'required': ['resource_id', 'query']
    })
    
    tool_id_and_name_mapping['get_knowledge_base_data'] = {
        "type": "RAG",
        "resource_to_collection_mapping": resource_to_collection_mapping
    }

def _should_enable_web_crawling_tool(built_in_tools):
    if not built_in_tools:
        return False
    return inbuild_tools["Gtwy_Web_Search"] in built_in_tools

def add_web_crawling_tool(tools, tool_id_and_name_mapping, built_in_tools, gtwy_web_search_filters=None):
    """Add Firecrawl-based web crawling tool when requested via built-in tools."""
    if not _should_enable_web_crawling_tool(built_in_tools):
        return

    tools.append({
        'type': 'function',
        'name': inbuild_tools["Gtwy_Web_Search"],
        'description': 'Search and extract content from any website URL. This tool scrapes web pages and returns their content in various formats. Use this when you need to: fetch real-time information from websites, extract article content, retrieve documentation, access public web data, or get current information not in your training data. If enum is provided for URL, only use URLs from those allowed domains.',
        'properties': {
            'url': {
                'description': 'The complete URL of the website to scrape (must start with http:// or https://). Example: https://example.com/page',
                'type': 'string',
                'enum': gtwy_web_search_filters if (gtwy_web_search_filters and len(gtwy_web_search_filters) > 0) else [],
                'required_params': [],
                'parameter': {}
            },
            'formats': {
                'description': 'Optional list of output formats. Available formats include: "markdown" (default, clean text), "html" (raw HTML), "screenshot" (visual capture), "links" (extracted URLs). If not specified, returns markdown format.',
                'type': 'array',
                'items': {
                    'type': 'string'
                },
                'enum': [],
                'required_params': [],
                'parameter': {}
            }
        },
        'required': ['url']
    })

    tool_id_and_name_mapping[inbuild_tools["Gtwy_Web_Search"]] = {
        'type': inbuild_tools["Gtwy_Web_Search"],
        'name': inbuild_tools["Gtwy_Web_Search"]
    }

def add_anthropic_json_schema(service, configuration, tools):
    """Add JSON schema response format for Anthropic service"""
    if (service != 'anthropic' or 
        not isinstance(configuration.get('response_type'), dict) or 
        not configuration['response_type'].get('json_schema')):
        return
    
    # Remove required field if it exists
    if configuration['response_type']['json_schema'].get('required') is not None:
        del configuration['response_type']['json_schema']['required']
    
    # Add JSON schema tool
    tools.append({
        "name": "JSON_Schema_Response_Format",
        "description": "return the response in json schema format",
        "input_schema": configuration.get('response_type').get('json_schema').get('schema')
    })
    
    # Update configuration
    configuration['response_type'] = 'default'
    configuration['prompt'] += '\n Always return the response in JSON SChema by calling the function JSON_Schema_Response_Format and if no values available then return json with dummy or default vaules'

def add_connected_agents(result, tools, tool_id_and_name_mapping, orchestrator_flag):
    """Add connected agents as tools"""
    connected_agents = result.get('bridges', {}).get('connected_agents', {})
    connected_agent_details = result.get('bridges', {}).get('connected_agent_details', {})
    
    if not connected_agents:
        return
    
    # Check if type is orchestrator
    is_orchestrator = orchestrator_flag or result.get('bridges', {}).get('orchestrator', False)
    
    for bridge_name, bridge_info in connected_agents.items():
        bridge_id_value = bridge_info.get('bridge_id', '')
        version_id_value = bridge_info.get('version_id', '')
        
        # If version_id is present, use connected_agents data, otherwise use connected_agent_details
        if version_id_value:
            # Use data from connected_agents when version_id is present
            description = bridge_info.get('description', '')
            variables = bridge_info.get('variables', {})
            fields = variables.get('fields', {})
            required_params = variables.get('required_params', [])
        else:
            # Use data from connected_agent_details when version_id is not present
            agent_details = connected_agent_details.get(bridge_id_value)
            if agent_details and agent_details is not None:
                description = agent_details.get('description', bridge_info.get('description', ''))
                variables = agent_details.get('agent_variables', {})
                fields = variables.get('fields', {})
                required_params = variables.get('required_params', [])
            else:
                # Final fallback to connected_agents data
                description = bridge_info.get('description', '')
                variables = bridge_info.get('variables', {})
                fields = variables.get('fields', {})
                required_params = variables.get('required_params', [])
        
        name = makeFunctionName(bridge_name)

        # Build properties dictionary
        properties = {
            "_query": {
                "description": "The query or message to be processed by the connected agent.",
                "type": "string",
                "enum": [],
                "required_params": [],
                "parameter": {}
            },
            **fields
        }
        
        # Add action_type only if type is orchestrator
        if is_orchestrator:
            properties["action_type"] = {
                "description": "transfer: directly return child agent response, conversation: get child response and continue processing",
                "type": "string",
                "enum": ["transfer", "conversation"],
                "required_params": [],
                "parameter": {}
            }
        
        # Build required list
        required = ["_query"] + required_params
        if is_orchestrator:
            required.append("action_type")

        tools.append({
            "type": "function",
            "name": name,
            "description": description,
            "properties": properties,
            "required": required
        })
        
        tool_id_and_name_mapping[name] = {
            "type": "AGENT",
            "bridge_id": bridge_id_value,
            "requires_thread_id": bridge_info.get('thread_id', False),
            "version_id": version_id_value
        }
