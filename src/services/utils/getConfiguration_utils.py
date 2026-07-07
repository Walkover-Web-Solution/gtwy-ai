import src.db_services.ConfigurationServices as ConfigurationService
from config import Config
from models.mongo_connection import db
from src.configs.constant import inbuild_tools
from src.services.commonServices.baseService.utils import makeFunctionName
from src.services.utils.common_utils import convert_prompt_to_string
from src.services.utils.helper import Helper
from src.services.utils.service_config_utils import tool_choice_function_name_formatter

apiCallModel = db["apicalls"]


async def validate_bridge(agent_data):
    """Validate bridge status and existence"""
    if not agent_data.get("success"):
        return {"success": False, "error": "Agent does not exist in this organization"}

    bridges = agent_data.get("bridges", {})

    if bridges.get("deletedAt"):
        return {"success": False, "error": "Agent has been deleted"}

    bridge_status = bridges.get("bridge_status")
    if bridge_status == 0:
        raise Exception("Agent is Currently Paused")

    return None


async def get_bridge_data(bridge_id, org_id, version_id, environment=None):
    """Fetch bridge data from database"""
    agent_data = await ConfigurationService.get_bridges_with_tools_and_apikeys(
        bridge_id=bridge_id, org_id=org_id, version_id=version_id, environment=environment
    )

    bridge_id = bridge_id or agent_data.get("bridges", {}).get("parent_id")

    return agent_data, bridge_id


def setup_configuration(configuration, bridges, service):
    """Setup and merge configuration from database and input"""
    db_configuration = bridges.get("configuration", {})
    service = service or (bridges.get("service", "").lower())

    if configuration:
        db_configuration.update(configuration)

    # Convert prompt dict (role/goal/instruction) to a proper string prompt
    prompt = db_configuration.get("prompt")
    folder_id = bridges.get("folder_id")

    if folder_id is not None and isinstance(prompt, dict):
        use_default = prompt.get("customPrompt")
        if use_default is None or use_default is True:
           db_configuration["prompt"] = convert_prompt_to_string(prompt)
        else:
            prompt_str = prompt.get("customPrompt")
            prompt_str, _ = Helper.replace_variables_in_prompt(prompt_str, prompt)
            db_configuration["prompt"] = prompt_str
    elif isinstance(prompt, dict):
        db_configuration["prompt"] = convert_prompt_to_string(prompt)

    return db_configuration, service


def setup_tool_choice(configuration, bridges, service):
    """Setup tool choice configuration"""
    tool_choice_ids = configuration.get("tool_choice", "")
    toolchoice = None

    # Find tool choice from API calls
    for _, api_data in bridges.get("apiCalls", {}).items():
        if api_data["_id"] in tool_choice_ids:
            toolchoice = api_data.get("title") or makeFunctionName(
                api_data["endpoint_name"] or api_data["function_name"]
            )
            break
    if not toolchoice:
        connected_agents_name = bridges.get("agent_name_info", {})
        agent_name = connected_agents_name.get(tool_choice_ids)
        if agent_name:
            toolchoice = makeFunctionName(agent_name)

    # Find choice type
    found_choice = None
    for choice in ["auto", "none", "required", "default", "any"]:
        if choice in tool_choice_ids:
            found_choice = choice
            break

    return tool_choice_function_name_formatter(
        service=service, configuration=configuration, toolchoice=toolchoice, found_choice=found_choice
    )


def process_api_call_tool(api_data, variables_path_bridge):
    """Process a single API call and convert it to a tool format"""
    name_of_function = api_data.get("title") or makeFunctionName(api_data["endpoint_name"] or api_data["function_name"])

    # Skip if status is paused and no function name
    if api_data.get("status") == 0 and not name_of_function:
        return None, None

    # Setup tool mapping
    tool_mapping = {
        "url": f"https://flow.sokt.io/func/{api_data.get('script_id')}",
        "headers": {},
        "name": api_data.get("script_id"),
        "method": "POST"
    }

    # Process variables filled by gateway
    variables_fill_by_gtwy = list(variables_path_bridge.get(api_data.get("script_id"), {}).keys())

    properties = api_data.get("fields", {})

    # Remove properties that are filled by gateway
    for key in variables_fill_by_gtwy:
        properties.pop(key, None)

    # Filter required parameters
    required = api_data.get("required", [])
    required = [key for key in required if key not in variables_fill_by_gtwy]

    # Create tool format
    tool_format = {
        "type": "function",
        "name": name_of_function,
        "description": api_data.get("description"),
        "properties": properties,
        "required": required,
    }

    return tool_format, tool_mapping


def process_extra_tool(tool):
    """Process an extra tool and convert it to tool format"""
    if not isinstance(tool, dict) or not tool.get("url"):
        return None, None, {}

    tool_name = tool.get("name")
    if not tool_name:
        return None, None, {}

    properties = tool.get("fields", {}) or {}
    if not isinstance(properties, dict):
        properties = {}

    required = tool.get("required", []) or []
    if not isinstance(required, list):
        required = []

    tool_format = {
        "type": "function",
        "name": makeFunctionName(tool_name),
        "description": tool.get("description"),
        "properties": properties,
        "required": required,
    }

    query_params = tool.get("query_params", []) or []
    if not isinstance(query_params, list):
        query_params = []
    tool_mapping = {
        "url": tool.get("url"),
        "headers": tool.get("headers", {}),
        "name": tool_name,
        "method": tool.get("method", "POST").upper(),
        "query_params": query_params,
    }
    variable_path = tool.get("tool_and_variable_path", {}) or {}
    # Remove properties that are filled by gateway
    for key in variable_path:
        properties.pop(key, None)

    return tool_format, tool_mapping, {tool_name: variable_path}


def _build_api_calls_map(api_calls):
    """Normalize apiCalls (dict or list) into a lookup map keyed by string id."""
    api_calls_map = {}
    if isinstance(api_calls, dict):
        for key, val in api_calls.items():
            api_calls_map[str(key)] = val
            if isinstance(val, dict) and val.get("_id"):
                api_calls_map[str(val["_id"])] = val
    elif isinstance(api_calls, list):
        for val in api_calls:
            if isinstance(val, dict) and val.get("_id"):
                api_calls_map[str(val["_id"])] = val
    return api_calls_map


def setup_tools(bridges, variables_path_bridge, extra_tools):
    """Setup tools by iterating connected_tools (type='tools') and looking up
    tool metadata from the joined apiCalls data."""
    tools = []
    tool_id_and_name_mapping = {}
    variable_path = {}

    # Iterate connected_tools where type="tools" — uses variable_path from each entry
    connected_tools = bridges.get("connected_tools", [])
    tool_entries = [ct for ct in connected_tools if ct.get("type") == "tools"]
    api_calls_map = _build_api_calls_map(bridges.get("apiCalls", {}))

    # Build variables_path from per-entry variable_path in connected_tools
    merged_variables_path = {}
    for tool_entry in tool_entries:
        tool_id = str(tool_entry.get("id", ""))
        api_data = api_calls_map.get(tool_id)
        if not api_data:
            continue
        entry_variable_path = tool_entry.get("variable_path", {}) or {}
        script_id = api_data.get("script_id")
        if entry_variable_path and script_id:
            existing = merged_variables_path.get(script_id, {}) or {}
            merged_variables_path[script_id] = {**existing, **entry_variable_path}

        tool_format, tool_mapping = process_api_call_tool(api_data, merged_variables_path)
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
    return tools, tool_id_and_name_mapping, {**merged_variables_path, **variable_path}


def setup_api_key(service, bridges, apikey, chatbot):
    """Setup API key for the service"""
    db_apikeys = bridges.get("apikeys", {})
    db_apikeys_object_id = bridges.get("apikey_object_id", {})
    # Get API key for the service
    db_api_key = db_apikeys.get(service)

    if service == "openai_completion":
        db_api_key = db_apikeys.get("openai")

    # Check for folder API keys if folder_id exists
    folder_api_key = bridges.get("folder_apikeys", {}).get(service)
    if folder_api_key:
        db_api_key = folder_api_key

    # Validate API key existence
    if chatbot and (service == "openai"):
        model = bridges.get("configuration", {}).get("model")
        # If both keys are not present
        if not (apikey or db_api_key):
            # Use Config.OPENAI_API_KEY only if model is gpt-5-nano
            if model == "gpt-5-nano":
                apikey = Config.OPENAI_API_KEY_GPT_5_NANO
            else:
                raise Exception("Could not find api key or Agent is not Published")

    if not (apikey or db_api_key):
        raise Exception("Could not find api key or Agent is not Published")

    # Handle fallback configuration
    fallback_config = bridges.get("settings", {}).get("fall_back")
    if fallback_config:
        fallback_service = fallback_config.get("service")
        fallback_apikey = db_apikeys.get(fallback_service)
        if fallback_apikey:
            if "settings" not in bridges:
                bridges["settings"] = {}
            if "fall_back" not in bridges["settings"]:
                bridges["settings"]["fall_back"] = {}
            bridges["settings"]["fall_back"]["apikey"] = Helper.decrypt(fallback_apikey)
            bridges["settings"]["fall_back"]["apikey_object_id"] = db_apikeys_object_id.get(fallback_service)

    # Use provided API key or decrypt from database
    return apikey if apikey else Helper.decrypt(db_api_key)


def setup_pre_tools(bridge, agent_data, variables):
    """Setup pre-tools configuration - reads from connected_tools type='pre_tool'"""
    connected_tools = bridge.get("connected_tools", [])
    pre_tool_entries = [ct for ct in connected_tools if ct.get("type") == "pre_tool"]
    if not pre_tool_entries:
        return None, None

    pre_tools_data = agent_data.get("bridges", {}).get("pre_tools_data", [])
    pre_tools_data_map = {pt.get("_id"): pt for pt in pre_tools_data}

    # Get the first pre_tool entry
    tool_entry = pre_tool_entries[0]
    tool_id = tool_entry.get("id")
    api_data = pre_tools_data_map.get(tool_id, {})
    if not api_data:
        raise Exception("Didn't find the pre_function")

    name = api_data.get("title") or makeFunctionName(api_data["endpoint_name"] or api_data["function_name"])
    required = api_data.get("required", [])

    # variable_path is embedded in the tool_entry
    variable_path = tool_entry.get("variable_path", {})
    args = {}
    for param in required:
        if param in variable_path:
            args[param] = variable_path[param]
        elif param in variables:
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
            resource_id = data.get("resource_id", "")
            collection_id = data.get("collection_id", "")
            if resource_id and collection_id:
                resource_to_collection_mapping[resource_id] = collection_id

    tools.append(
        {
            "type": "function",
            "name": "get_knowledge_base_data",
            "description": "When user want to take any data from the knowledge, Call this function to get the corresponding resource id",
            "properties": {
                "resource_id": {
                    "description": "send resource id",
                    "type": "string",
                    "enum": [],
                    "required": [],
                    "parameter": {},
                },
                "query": {
                    "description": "query to ask from the knowledge base",
                    "type": "string",
                    "enum": [],
                    "required": [],
                    "parameter": {},
                },
            },
            "required": ["resource_id", "query"],
        }
    )

    tool_id_and_name_mapping["get_knowledge_base_data"] = {
        "type": "RAG",
        "resource_to_collection_mapping": resource_to_collection_mapping,
    }


def _should_enable_web_crawling_tool(built_in_tools):
    if not built_in_tools:
        return False
    return inbuild_tools["Gtwy_Web_Search"] in built_in_tools


def add_web_crawling_tool(tools, tool_id_and_name_mapping, built_in_tools, gtwy_web_search_filters=None):
    """Add Firecrawl-based web crawling tool when requested via built-in tools."""
    if not _should_enable_web_crawling_tool(built_in_tools):
        return

    tools.append(
        {
            "type": "function",
            "name": inbuild_tools["Gtwy_Web_Search"],
            "description": "Search and extract content from any website URL. This tool scrapes web pages and returns their content in various formats. Use this when you need to: fetch real-time information from websites, extract article content, retrieve documentation, access public web data, or get current information not in your training data. If enum is provided for URL, only use URLs from those allowed domains.",
            "properties": {
                "url": {
                    "description": "The complete URL of the website to scrape (must start with http:// or https://). Example: https://example.com/page",
                    "type": "string",
                    "enum": gtwy_web_search_filters
                    if (gtwy_web_search_filters and len(gtwy_web_search_filters) > 0)
                    else [],
                    "required": [],
                    "parameter": {},
                },
                "formats": {
                    "description": 'Optional list of output formats. Available formats include: "markdown" (default, clean text), "html" (raw HTML), "screenshot" (visual capture), "links" (extracted URLs). If not specified, returns markdown format.',
                    "type": "array",
                    "items": {"type": "string"},
                    "enum": [],
                    "required": [],
                    "parameter": {},
                },
            },
            "required": ["url"],
        }
    )

    tool_id_and_name_mapping[inbuild_tools["Gtwy_Web_Search"]] = {
        "type": inbuild_tools["Gtwy_Web_Search"],
        "name": inbuild_tools["Gtwy_Web_Search"],
    }


def add_connected_agents(bridges, tools, tool_id_and_name_mapping, orchestrator_flag):
    """Add connected agents as tools - reads from connected_tools type='agent'"""
    connected_tools = bridges.get("connected_tools", [])
    agent_entries = [ct for ct in connected_tools if ct.get("type") == "agent"]
    connected_agent_details = bridges.get("connected_agent_details", {})
    agent_name_info = bridges.get("agent_name_info", {})

    if not agent_entries:
        return

    # Check if type is orchestrator
    is_orchestrator = orchestrator_flag or bridges.get("orchestrator", False)

    for agent_entry in agent_entries:
        bridge_id_value = agent_entry.get("id", "")
        environment_value = agent_entry.get("environment", "")
        # If environment is present, use agent_entry data, otherwise use connected_agent_details
        if environment_value:
            description = agent_entry.get("description", "")
            variables = agent_entry.get("variables", {})
            fields = variables.get("fields", {})
            required = variables.get("required", [])
        else:
            agent_details = connected_agent_details.get(bridge_id_value) or {}
            if agent_details:
                description = agent_details.get("description", agent_entry.get("description", ""))
                variables = agent_details.get("agent_variables", {})
                fields = variables.get("fields", {})
                required = variables.get("required", [])
            else:
                # Final fallback to agent_entry data
                description = agent_entry.get("description", "")
                variables = agent_entry.get("variables", {})
                fields = variables.get("fields", {})
                required = variables.get("required", [])

        name = makeFunctionName(agent_name_info.get(bridge_id_value, ""))

        # Build properties dictionary
        properties = {
            "_query": {
                "description": "The query or message to be processed by the connected agent.",
                "type": "string",
                "enum": [],
                "required": [],
                "parameter": {},
            },
            **fields,
        }

        # Add action_type only if type is orchestrator
        if is_orchestrator:
            properties["action_type"] = {
                "description": "transfer: directly return child agent response, conversation: get child response and continue processing",
                "type": "string",
                "enum": ["transfer", "conversation"],
                "required": [],
                "parameter": {},
            }

        # Build required list
        required = ["_query"] + required
        if is_orchestrator:
            required.append("action_type")

        tools.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "properties": properties,
                "required": required,
            }
        )

        tool_id_and_name_mapping[name] = {
            "type": "AGENT",
            "bridge_id": bridge_id_value,
            "requires_thread_id": agent_entry.get("thread_id", False),
        }
