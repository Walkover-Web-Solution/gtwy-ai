"""Response formatter for the Grok (xAI) service (OpenAI-chat shape, no cached tokens)."""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_grok(response, tools_data, images=None):
    return {
        "data": {
            "id": response.get("id", None),
            "content": response.get("choices", [{}])[0].get("message", {}).get("content", None),
            "model": response.get("model", None),
            "role": response.get("choices", [{}])[0].get("message", {}).get("role", None),
            "tools_data": tools_data or {},
            "images": images,
            "annotations": response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", "")),
        },
        "usage": {
            "input_tokens": response.get("usage", {}).get("prompt_tokens", None),
            "output_tokens": response.get("usage", {}).get("completion_tokens", None),
            "total_tokens": response.get("usage", {}).get("total_tokens", None),
        },
    }
