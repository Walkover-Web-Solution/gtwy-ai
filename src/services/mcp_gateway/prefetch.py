import asyncio
import hashlib
import json

from globals import logger
from src.services.cache_service import find_in_cache, store_in_cache

from .transport import open_mcp_session


_TOOL_LIST_TTL = 600  # 10 minutes
_CACHE_PREFIX = "mcp_tools_list:"


def _cache_key(url: str, headers: dict) -> str:
    payload = json.dumps({"url": url, "headers": headers or {}}, sort_keys=True)
    return _CACHE_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


def _tool_to_dict(tool) -> dict:
    schema = getattr(tool, "inputSchema", None)
    if schema is None and hasattr(tool, "model_dump"):
        schema = tool.model_dump().get("inputSchema")
    return {
        "name": getattr(tool, "name", None),
        "description": getattr(tool, "description", "") or "",
        "inputSchema": schema or {},
    }


async def _fetch_server_tools(url: str, headers: dict) -> list[dict]:
    cache_key = _cache_key(url, headers)
    cached = await find_in_cache(cache_key)
    if cached:
        try:
            parsed = json.loads(cached)
            if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list):
                return parsed["tools"]
        except (json.JSONDecodeError, ValueError):
            pass

    async with open_mcp_session(url, headers) as session:
        result = await session.list_tools()

    tools = [_tool_to_dict(t) for t in (getattr(result, "tools", None) or [])]
    await store_in_cache(cache_key, {"tools": tools}, ttl=_TOOL_LIST_TTL)
    return tools


async def _populate(server: dict) -> None:
    if not isinstance(server, dict):
        return
    if server.get("tools"):
        return
    url = server.get("url")
    if not url:
        return
    try:
        server["tools"] = await _fetch_server_tools(url, server.get("headers") or {})
    except Exception as err:
        logger.error(
            f"prefetch_mcp_tools: list_tools failed for {server.get('name')!r} ({url}): {err}"
        )
        server["tools"] = []


async def prefetch_mcp_tools(mcp_config: dict) -> None:
    """Populate ``server['tools']`` on each MCP server in-place via list_tools().

    No-op when ``mcp_config`` is missing, disabled, has no servers, or every
    server already carries pre-fetched schemas. Results are cached in Redis
    keyed on URL+headers for 10 minutes.
    """
    if not isinstance(mcp_config, dict):
        return
    if not mcp_config.get("enabled"):
        return
    servers = mcp_config.get("servers") or []
    if not servers:
        return
    await asyncio.gather(*(_populate(s) for s in servers))
