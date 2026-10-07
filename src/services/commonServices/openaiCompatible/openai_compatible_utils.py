import json

from src.services.utils.web_search_config import (
    build_web_search_tool,
    merge_extra_body,
    use_web_search,
    web_search_extra_body,
)


def apply_openai_compatible_builtin_tools(custom_config, service, model, built_in_tools):
    """Add provider-native web search when the agent asks for it and the model supports it.

    Both come from the DB: services.web_search_tool gives the tool entry (Moonshot $web_search,
    MiniMax web_search) or the extra_body addition (OpenRouter's web plugin), and the model's
    validationConfig.inbuilt_tools.web_search flag says whether it supports it. Models without the
    flag must not get the tool: MiniMax M2.x, for example, leaks its internal plugin_web_search as a
    plain function call instead of running the search.
    """
    if not use_web_search(service, model, built_in_tools):
        return custom_config

    extra_body = web_search_extra_body(service)
    if extra_body:
        custom_config["extra_body"] = merge_extra_body(custom_config.get("extra_body"), extra_body)

    web_search_tool = build_web_search_tool(service)
    if web_search_tool:
        custom_config.setdefault("tools", []).append(web_search_tool)
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
    web_search_calls = []
    for tool_call in tool_calls:
        if tool_call.get("function", {}).get("name") != "$web_search":
            continue

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
        web_search_calls.append({"id": tool_call.get("id"), "arguments": tool_result})

    return tools, web_search_calls
