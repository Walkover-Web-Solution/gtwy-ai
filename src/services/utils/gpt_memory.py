import json
from typing import Any

from globals import logger
from src.services.cache_service import find_in_cache, store_in_cache

from ..utils.apiservice import fetch

MEMORY_SCHEMA_VERSION = 2
_META_KEYS = frozenset({"version", "updated_at", "pages"})


def _deserialize_cached_value(raw_value):
    """Decode Redis payloads into native python structures."""
    if raw_value is None:
        return None
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8")
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def _is_empty_memory_response(value) -> bool:
    """Sokt flow returns {'success': True, 'message': 'No response'} when no memory exists for the thread."""
    return (
        isinstance(value, dict)
        and value.get("success") is True
        and value.get("message") == "No response"
    )


def _build_memory_document(pages: dict, updated_at: Any = None) -> dict:
    doc = {"version": MEMORY_SCHEMA_VERSION, "pages": pages if isinstance(pages, dict) else {}}
    if updated_at is not None:
        doc["updated_at"] = updated_at
    return doc


def normalize_memory_to_pages(parsed: Any) -> dict | None:
    """
    Normalize any memory payload into:
      { "version": 2, "pages": { "<key>": <content>, ... }, "updated_at"?: ... }
    Returns None when there is no usable memory.
    """
    if parsed is None or _is_empty_memory_response(parsed):
        return None

    if isinstance(parsed, str):
        text = parsed.strip()
        if not text:
            return None
        return _build_memory_document({"legacy": text})

    if not isinstance(parsed, dict):
        return _build_memory_document({"legacy": str(parsed)})

    updated_at = parsed.get("updated_at")

    # Already page-shaped: { version?, pages: {...}, updated_at? }
    pages = parsed.get("pages")
    if isinstance(pages, dict):
        if not pages:
            return None
        return _build_memory_document(pages, updated_at=updated_at)

    # Top-level content dict (e.g. { "behaviour": ..., "facts": ... }) — treat keys as pages
    content_pages = {k: v for k, v in parsed.items() if k not in _META_KEYS}
    if content_pages:
        return _build_memory_document(content_pages, updated_at=updated_at)

    return None


def parse_memory(raw):
    """
    Normalize a memory payload to structured JSON pages form, or None.

    Shape:
      { "version": 2, "pages": { ... }, "updated_at"?: ... }

    Legacy strings become pages.legacy. Dicts with a pages key, or top-level
    page keys (behaviour/facts/...), are normalized the same way.
    """
    parsed = _deserialize_cached_value(raw)
    return normalize_memory_to_pages(parsed)


def get_memory_page_keys(memory: dict | None) -> list[str]:
    """Return top-level page keys from a normalized memory document."""
    pages = memory.get("pages") if isinstance(memory, dict) else None
    if isinstance(pages, dict):
        return list(pages.keys())
    return []


def _outline_page_value(value: Any, *, max_chars: int = 120) -> str:
    """Short outline of a page value for the agent prompt (not full content)."""
    if value is None:
        return "empty"
    if isinstance(value, dict):
        if not value:
            return "empty object"
        nested = ", ".join(str(k) for k in list(value.keys())[:12])
        extra = "" if len(value) <= 12 else f", +{len(value) - 12} more"
        return f"object with fields: {nested}{extra}"
    if isinstance(value, list):
        if not value:
            return "empty list"
        return f"list with {len(value)} item(s)"
    if isinstance(value, bool):
        return f"boolean ({value})"
    if isinstance(value, (int, float)):
        return f"number ({value})"
    text = str(value).strip().replace("\n", " ")
    if not text:
        return "empty string"
    if len(text) > max_chars:
        return f"text (~{len(text)} chars): {text[:max_chars]}…"
    return f"text: {text}"


def summarize_memory_for_prompt(memory: dict | None) -> list[dict[str, str]]:
    """
    Per-page inventory for the system prompt: key + short data outline.
    Does not dump full page payloads — only enough for the agent to choose keys.
    """
    pages = memory.get("pages") if isinstance(memory, dict) else None
    if isinstance(pages, dict):
        return [
            {"key": str(key), "outline": _outline_page_value(value)}
            for key, value in pages.items()
        ]
    return []


def get_memory_pages(memory: dict | None, keys: list[str] | None = None, include_all: bool = False) -> dict:
    """
    Return a slice of memory pages.

    - include_all=True → every page
    - keys=[...] → only those keys that exist
    - otherwise → {}
    """
    pages = memory.get("pages") if isinstance(memory, dict) else None
    if isinstance(pages, dict):
        if include_all:
            return dict(pages)
        if keys:
            return {key: pages[key] for key in keys if key in pages}
    return {}


async def _fetch_memory_from_cache(memory_id: str):
    cached_value = await find_in_cache(memory_id)
    return _deserialize_cached_value(cached_value)


async def _fetch_memory_from_remote(memory_id: str):
    try:
        response, _ = await fetch("https://flow.sokt.io/func/scriCJLHynCG", "POST", None, None, {"threadID": memory_id})
        if response is None or _is_empty_memory_response(response):
            return None
        await store_in_cache(memory_id, response)
        return response
    except Exception as err:
        logger.error(f"Error fetching GPT memory from remote for {memory_id}: {str(err)}")
        return None


def _build_memory_id(thread_id: str, sub_thread_id: str, bridge_id: str, version_id: str | None) -> str:
    version_or_bridge = (version_id or bridge_id or "").strip()
    return f"{thread_id.strip()}_{sub_thread_id.strip()}_{version_or_bridge}"


async def get_gpt_memory(
    bridge_id: str, thread_id: str, sub_thread_id: str, version_id: str | None = None
) -> tuple[str, Any | None]:
    """Return GPT memory content for the provided identifiers."""
    memory_id = _build_memory_id(thread_id, sub_thread_id, bridge_id, version_id)
    memory = await _fetch_memory_from_cache(memory_id)
    if memory is None:
        memory = await _fetch_memory_from_remote(memory_id)

    return memory_id, memory
