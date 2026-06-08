"""Shared response formatter for plain OpenAI-Chat-compatible services.

Covers open_router, neev_cloud, moonshot, openai_completion, mistral, and any
future openai_sdk service. groq / grok / deepseek have their own formatters
(reasoning surfacing / cached-token differences).
"""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_openai_compatible(response, tools_data, images=None):
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
            "cached_tokens": (response.get("usage", {}).get("prompt_tokens_details", {}) or {}).get(
                "cached_tokens"
            ),
        },
    }
