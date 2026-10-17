"""Response formatter for the Anthropic service (chat + batch)."""

from src.services.utils.formatters.finish_reason import finish_reason_mapping
from src.services.utils.formatters.web_search_extractor import extract_web_search_annotations


def _join_text(content_blocks):
    # With web search the answer is split into several text blocks (one per cited span). Adjacent
    # blocks join directly; text separated by a search/tool block starts a new paragraph.
    text, separated = "", False
    for block in content_blocks or []:
        if block.get("type") != "text":
            separated = bool(text)
            continue
        if block.get("text"):
            text += ("\n\n" if separated else "") + block["text"]
            separated = False
    return text or None


def format_anthropic(response, tools_data, images, isBatch=False):
    if isBatch:
        return _format_batch(response, tools_data, images)
    return _format_chat(response, tools_data, images)


def _format_batch(response, tools_data, images):
    # Anthropic batch responses follow standard Anthropic message format
    content_blocks = response.get("content", [])
    text_content = _join_text(content_blocks)
    return {
        "data": {
            "id": response.get("id", None),
            "content": text_content,
            "model": response.get("model", None),
            "role": response.get("role", "assistant"),
            "tools_data": tools_data or {},
            "images": images,
            "annotations": extract_web_search_annotations(response, "anthropic") or None,
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
    text_content = _join_text(content_blocks)
    thinking_content = next((b.get("thinking") for b in content_blocks if b.get("type") == "thinking"), None)
    return {
        "data" : {
            "id" : response.get("id", None),
            "content" : text_content,
            "reasoning": thinking_content,
            "model" : response.get("model", None),
            "role" : response.get("role", None),
            "tools_data": tools_data or {},
            "annotations": extract_web_search_annotations(response, "anthropic") or None,
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
