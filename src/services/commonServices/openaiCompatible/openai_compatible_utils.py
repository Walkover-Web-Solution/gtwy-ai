import json
from copy import deepcopy

from src.configs.model_configuration import model_config_document


OPENAI_COMPATIBLE_BUILTIN_TOOLS = {
    "moonshot": {
        "web_search": {
            "type": "builtin_function",
            "function": {
                "name": "$web_search",
            },
        },
    },
}


def apply_openai_compatible_builtin_tools(custom_config, service, model, built_in_tools):
    if not built_in_tools:
        return custom_config

    service_tools = OPENAI_COMPATIBLE_BUILTIN_TOOLS.get(service, {})
    if not service_tools:
        return custom_config

    model_configuration = model_config_document.get(service, {}).get(model, {}).get("configuration", {})
    if "tools" not in model_configuration:
        return custom_config

    configured_tools = [
        deepcopy(service_tools[tool])
        for tool in built_in_tools
        if tool in service_tools
    ]
    if not configured_tools:
        return custom_config

    if "tools" not in custom_config:
        custom_config["tools"] = []

    custom_config["tools"].extend(configured_tools)
    return custom_config


def get_openai_compatible_tool_calls(model_response):
    return model_response.get("choices", [{}])[0].get("message", {}).get("tool_calls", []) or []


def has_moonshot_web_search_tool_calls(service, model_response):
    tool_calls = get_openai_compatible_tool_calls(model_response)
    return service == "moonshot" and any(
        tool_call.get("function", {}).get("name") == "$web_search"
        for tool_call in tool_calls
    )


def append_moonshot_web_search_tool_results(configuration, model_response):
    message = model_response.get("choices", [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls", []) or []
    configuration["messages"].append(message)

    tools = {}
    web_search_count = 0
    for tool_call in tool_calls:
        if tool_call.get("function", {}).get("name") != "$web_search":
            continue

        web_search_count += 1
        raw_arguments = tool_call.get("function", {}).get("arguments")
        try:
            tool_result = json.loads(raw_arguments) if isinstance(raw_arguments, str) else (raw_arguments or {})
        except (json.JSONDecodeError, TypeError):
            tool_result = {}

        content = json.dumps(tool_result)
        configuration["messages"].append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "name": "$web_search",
                "content": content,
            }
        )
        tools["$web_search"] = content

    return tools, web_search_count
