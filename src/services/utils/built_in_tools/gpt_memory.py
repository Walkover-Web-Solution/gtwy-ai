"""Built-in get_gpt_memory tool — local executor (same pattern as firecrawl)."""

from __future__ import annotations

import json

from src.services.utils.gpt_memory import get_memory_page_keys, get_memory_pages


async def call_get_gpt_memory(args, memory):
    """
    Execute get_gpt_memory from LLM tool-call args.

    Args:
      keys: list[str] — page keys to return
      all: bool — if true, return every page (keys ignored)

    Returns the standard tool result shape used by baseService.
    """
    args = args if isinstance(args, dict) else {}
    include_all = bool(args.get("all"))
    keys = args.get("keys")
    if keys is not None and not isinstance(keys, list):
        keys = [keys]

    available = get_memory_page_keys(memory)
    if not available:
        return {
            "response": {"error": "No GPT memory available for this thread.", "available_keys": []},
            "metadata": {"type": "function"},
            "status": 0,
        }

    if not include_all and not keys:
        return {
            "response": {
                "error": "Provide keys or set all=true.",
                "available_keys": available,
            },
            "metadata": {"type": "function"},
            "status": 0,
        }

    pages = get_memory_pages(memory, keys=keys, include_all=include_all)
    try:
        json.dumps(pages)
    except (TypeError, ValueError):
        pages = json.loads(json.dumps(pages, default=str))

    return {
        "response": {
            "pages": pages,
            "available_keys": available,
        },
        "metadata": {"type": "function"},
        "status": 1,
    }
