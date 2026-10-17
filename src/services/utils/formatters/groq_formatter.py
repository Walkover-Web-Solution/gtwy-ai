"""Response formatter for the Groq service (OpenAI-chat shape, surfaces reasoning)."""

from src.services.utils.formatters.finish_reason import finish_reason_mapping
from src.services.utils.formatters.web_search_extractor import extract_web_search_annotations


def format_groq(response, tools_data, images=None):
    message = response.get("choices", [{}])[0].get("message", {})
    usage = response.get("usage", {})
    return {
        "data": {
            "id": response.get("id", None),
            "content": message.get("content", None),
            "reasoning": message.get("reasoning", None),
            "model": response.get("model", None),
            "role": message.get("role", None),
            "tools_data": tools_data or {},
            "annotations": extract_web_search_annotations(response, "groq") or None,
            "fallback": response.get("fallback") or False,
            "finish_reason": finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", "")),
        },
        "usage": {
            "input_tokens": usage.get("prompt_tokens", None),
            "output_tokens": usage.get("completion_tokens", None),
            "total_tokens": usage.get("total_tokens", None),
            "reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens", None),
        },
    }
