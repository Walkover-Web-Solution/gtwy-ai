"""Response formatter for the TypeSafe (Jev) service.

Raw response: ``{"model": "jev-1.13.0", "answers": {id: {...}}, "usage": {"input_tokens": n, "output_tokens": m}}``.

``data.content`` carries the answers as a JSON string so every text-based
consumer (history, playground, JSON repair) keeps working; ``data.answers``
carries the same map untouched for API callers.
"""

import json

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_typesafe(response, tools_data, images=None):
    answers = response.get("answers") or {}
    usage = response.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)

    return {
        "data": {
            "id": response.get("request_id") or response.get("id"),
            "content": json.dumps(answers),
            "answers": answers,
            "model": response.get("model"),
            "role": "assistant",
            "tools_data": tools_data or {},
            "images": images,
            "annotations": None,
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping("stop"),
        },
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached_tokens": 0,
        },
    }
