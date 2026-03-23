"""
Shared streaming generator for OpenAI-compatible chat.completions APIs.

Used by: groq, openrouter, ai_ml
Not used by: mistral (different SDK/event shape), grok (raw httpx SSE)
"""


async def openai_compat_stream(client, configuration):
    """Async generator yielding normalised delta dicts from any OpenAI-compatible
    chat.completions streaming endpoint.

    Yields intermediate deltas:
        {"content": str, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}

    Yields a single terminal chunk after the stream ends:
        {"content": None, "tool_calls": list|None, "usage": dict, "finish_reason": str|None, "reasoning": None}

    On error yields:
        {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str}
    """
    config = {
        **configuration,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    accumulated_tool_calls = {}
    usage = {}
    finish_reason = None
    try:
        stream = await client.chat.completions.create(**config)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if chunk.usage:
                usage = chunk.usage.to_dict() if hasattr(chunk.usage, "to_dict") else dict(chunk.usage)
            if not choice:
                continue
            delta = choice.delta
            finish_reason = choice.finish_reason or finish_reason
            if delta.content:
                yield {"content": delta.content, "tool_calls": None, "usage": None, "finish_reason": None, "reasoning": None}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {
                            "id": tc.id or "",
                            "name": (tc.function.name or "") if tc.function else "",
                            "arguments": "",
                        }
                    if tc.function and tc.function.arguments:
                        accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
        tool_calls_list = [
            {"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in accumulated_tool_calls.values()
        ] if accumulated_tool_calls else None
        yield {"content": None, "tool_calls": tool_calls_list, "usage": usage, "finish_reason": finish_reason, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}
