"""Response formatter for the Anthropic service (chat + batch)."""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_anthropic(response, tools_data, images, isBatch=False):
    if isBatch:
        return _format_batch(response, tools_data, images)
    return _format_chat(response, tools_data, images)


def _format_batch(response, tools_data, images):
    # Anthropic batch responses follow standard Anthropic message format
    content_blocks = response.get("content", [])
    text_content = next((block.get("text") for block in content_blocks if block.get("type") == "text"), None)
    return {
        "data": {
            "id": response.get("id", None),
            "content": text_content,
            "model": response.get("model", None),
            "role": response.get("role", "assistant"),
            "tools_data": tools_data or {},
            "images": images,
            "annotations": None,
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping(response.get("stop_reason", "")),
        },
        "usage": {
            "input_tokens": response.get("usage", {}).get("input_tokens", 0),
            "output_tokens": response.get("usage", {}).get("output_tokens", 0),
            "total_tokens": (
                response.get("usage", {}).get("input_tokens", 0) + response.get("usage", {}).get("output_tokens", 0)
            ),
            "cache_read_input_tokens": response.get("usage", {}).get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": response.get("usage", {}).get("cache_creation_input_tokens", 0),
        },
    }


def _format_chat(response, tools_data, images):
    content_blocks = response.get("content", [])
    text_content = next((b.get("text") for b in content_blocks if b.get("type") == "text"), None)
    thinking_content = next((b.get("thinking") for b in content_blocks if b.get("type") == "thinking"), None)
    return {
        "data" : {
            "id" : response.get("id", None),
            "content" : text_content,
            "reasoning": thinking_content,
            "model" : response.get("model", None),
            "role" : response.get("role", None),
            "tools_data": tools_data or {},
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping(response.get("stop_reason", "")),
        },
        "usage": {
            "input_tokens": response.get("usage", {}).get("input_tokens", None),
            "output_tokens": response.get("usage", {}).get("output_tokens", None),
            "cache_read_input_tokens": response.get("usage", {}).get("cache_read_input_tokens", None),
            "cache_creation_input_tokens": response.get("usage", {}).get("cache_creation_input_tokens", None),
            "total_tokens": (
                response.get("usage", {}).get("input_tokens", 0) + response.get("usage", {}).get("output_tokens", 0)
            ),
        },
    }
