import json

from globals import logger

from .transport import describe_mcp_error, open_mcp_session, outer_task_cancelled


def _extract_content(result) -> str:
    content = getattr(result, "content", None)
    if not content:
        return ""
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        data = getattr(item, "data", None)
        if data is not None:
            try:
                parts.append(json.dumps(data))
            except (TypeError, ValueError):
                parts.append(str(data))
            continue
        parts.append(str(item))
    return "\n".join(parts)


async def call_mcp_tool(args: dict, tool_info: dict) -> dict:
    url = tool_info.get("mcp_url")
    headers = tool_info.get("mcp_headers") or {}
    real_name = tool_info.get("mcp_tool")
    server_name = tool_info.get("mcp_server")

    if not url or not real_name:
        return {
            "response": "MCP tool registration is incomplete (missing url or tool name).",
            "metadata": {"type": "mcp", "server": server_name, "tool": real_name},
            "status": 0,
        }

    try:
        async with open_mcp_session(url, headers) as session:
            result = await session.call_tool(real_name, args or {})

        if getattr(result, "isError", False):
            return {
                "response": _extract_content(result) or "MCP tool returned an error.",
                "metadata": {"type": "mcp", "server": server_name, "tool": real_name},
                "status": 0,
            }

        return {
            "response": _extract_content(result),
            "metadata": {"type": "mcp", "server": server_name, "tool": real_name},
            "status": 1,
        }
    except BaseException as error:
        # Transport failures (e.g. a 401 on tools/call) arrive as BaseExceptionGroups;
        # turn them into a tool error instead of letting them escape the tool run.
        if outer_task_cancelled() or isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        message = describe_mcp_error(error)
        logger.error(f"call_mcp_tool: {server_name}/{real_name} failed: {message}")
        return {
            "response": message,
            "metadata": {"type": "mcp", "server": server_name, "tool": real_name},
            "status": 0,
        }
