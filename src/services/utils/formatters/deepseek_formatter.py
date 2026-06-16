"""Response formatter for the DeepSeek service.

OpenAI-chat shape, but surfaces reasoning from ``reasoning_content``.
"""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_deepseek(response, tools_data, images=None):
    message = response.get("choices", [{}])[0].get("message", {})
    return {
        "data": {
            "id": response.get("id", None),
            "content": message.get("content", None),
            "reasoning": message.get("reasoning_content", None),
            "model": response.get("model", None),
            "role": message.get("role", None),
            "tools_data": tools_data or {},
            "images": images,
            "annotations": message.get("annotations", None),
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", "")),
        },
        "usage": {
            "input_tokens": response.get("usage", {}).get("prompt_tokens", None),
            "output_tokens": response.get("usage", {}).get("completion_tokens", None),
            "total_tokens": response.get("usage", {}).get("total_tokens", None),
            "reasoning_tokens": (response.get("usage", {}).get("completion_tokens_details") or {}).get(
                "reasoning_tokens", None
            ),
        },
    }
