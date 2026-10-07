"""Convert between the OpenAI chat-completions shape and the OpenAI Responses API.

Some providers only expose their server-side ``web_search`` tool on a Responses endpoint
(xAI Grok, MiniMax), while our pipeline (handlers, function-call loop, formatters, token
calculator) works on ``choices[0].message``. A runner that sees the web_search tool converts
the chat request with ``to_responses_request``, calls the provider's Responses endpoint, and
maps the result back with ``to_chat_completion`` (text, function calls, url_citation
annotations, usage, and the web search queries/count).
"""

import json

# Chat-completions keys that map straight onto the Responses API.
_PASSTHROUGH_KEYS = ("model", "temperature", "top_p", "parallel_tool_calls", "user")


def _convert_content(content):
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image_url = part.get("image_url")
            parts.append({"type": "input_image", "image_url": image_url.get("url") if isinstance(image_url, dict) else image_url})
        else:
            parts.append(part)
    return parts


def _convert_messages(messages):
    items = []
    for message in messages or []:
        role = message.get("role")
        if role == "tool":
            output = message.get("content")
            items.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id"),
                "output": output if isinstance(output, str) else json.dumps(output),
            })
            continue
        if role == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "{}",
                })
            continue
        items.append({"role": role, "content": _convert_content(message.get("content"))})
    return items


def _convert_tools(tools):
    converted = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            converted.append({
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            })
        else:
            converted.append(tool)
    return converted


def _convert_tool_choice(tool_choice):
    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
        return {"type": "function", "name": tool_choice["function"].get("name")}
    return tool_choice


def _convert_response_format(response_format):
    if not isinstance(response_format, dict):
        return None
    if response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema") or {}
        return {"format": {"type": "json_schema", **schema}}
    if response_format.get("type") == "json_object":
        return {"format": {"type": "json_object"}}
    return None


def to_responses_request(configuration):
    """Build a /v1/responses body from a chat-completions configuration."""
    body = {key: configuration[key] for key in _PASSTHROUGH_KEYS if key in configuration}
    body["input"] = _convert_messages(configuration.get("messages"))
    body["tools"] = _convert_tools(configuration.get("tools"))
    if configuration.get("tool_choice") is not None:
        body["tool_choice"] = _convert_tool_choice(configuration["tool_choice"])
    max_tokens = configuration.get("max_completion_tokens") or configuration.get("max_tokens")
    if max_tokens:
        body["max_output_tokens"] = max_tokens
    text = _convert_response_format(configuration.get("response_format"))
    if text:
        body["text"] = text
    if configuration.get("reasoning_effort"):
        body["reasoning"] = {"effort": configuration["reasoning_effort"]}
    return body


def count_web_searches(response):
    return sum(1 for item in response.get("output") or [] if item.get("type") == "web_search_call")


def function_tool_names(configuration):
    """Names of the caller's own function tools in a chat-completions configuration."""
    return {
        (tool.get("function") or {}).get("name")
        for tool in configuration.get("tools") or []
        if isinstance(tool, dict) and tool.get("type") == "function"
    }


def to_chat_completion(response, function_names=None):
    """Convert a /v1/responses result into the chat-completions shape the pipeline expects.

    ``function_names``: when given, only function calls to these tools become tool_calls. Providers
    can leak internal server plugins as plain function calls (MiniMax M2.x returns
    ``plugin_web_search`` instead of running the search); those must never reach the tool loop.
    """
    texts, annotations, tool_calls, reasoning = [], [], [], []
    queries = [
        (item.get("action") or {}).get("query")
        for item in response.get("output") or []
        if item.get("type") == "web_search_call" and (item.get("action") or {}).get("query")
    ]
    default_query = queries[0] if queries else ""
    for item in response.get("output") or []:
        item_type = item.get("type")
        if item_type == "message":
            for content in item.get("content") or []:
                if content.get("type") != "output_text":
                    continue
                texts.append(content.get("text") or "")
                for annotation in content.get("annotations") or []:
                    if annotation.get("type") == "url_citation":
                        title = annotation.get("title") or ""
                        entry = {
                            "type": "url_citation",
                            # xAI puts the citation number in title; the url is the useful label
                            "title": annotation.get("url") if title.isdigit() else title,
                            "url": annotation.get("url") or "",
                            "query": default_query,
                        }
                        if annotation.get("content"):
                            # MiniMax includes the cited snippet with each citation
                            entry["cited_text"] = [annotation["content"]]
                        annotations.append(entry)
        elif item_type == "function_call":
            if function_names is not None and item.get("name") not in function_names:
                continue
            tool_calls.append({
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"},
            })
        elif item_type == "reasoning":
            reasoning.extend(s.get("text", "") for s in item.get("summary") or [] if isinstance(s, dict))

    # Top-level citations list every source the search touched; use it when no inline citations came back
    if not annotations:
        annotations = [
            {"type": "url_citation", "title": url, "url": url, "query": default_query}
            for url in response.get("citations") or []
            if isinstance(url, str)
        ]

    message = {"role": "assistant", "content": "".join(texts) if texts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if annotations:
        message["annotations"] = annotations
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)

    if tool_calls:
        finish_reason = "tool_calls"
    elif response.get("status") == "incomplete":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    usage = response.get("usage") or {}
    return {
        "id": response.get("id"),
        "object": "chat.completion",
        "model": response.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens_details": usage.get("input_tokens_details") or {},
            "completion_tokens_details": usage.get("output_tokens_details") or {},
        },
        "citations": response.get("citations") or [],
        "web_search_count": count_web_searches(response),
        "web_search_queries": queries,
    }
