import uuid

from db.json_store import now_str, store_delete, store_get, store_list, store_put, store_update


def _generate_tool_id() -> str:
    return f"tool_{uuid.uuid4().hex[:12]}"


async def create_tool(tool_data: dict) -> dict:
    """Create a new tool definition."""
    now = now_str()
    tool = {
        "tool_id": _generate_tool_id(),
        "org_id": tool_data.get("org_id", "default"),
        "name": tool_data["name"],
        "title": tool_data.get("title") or tool_data["name"],
        "description": tool_data.get("description", ""),
        "type": tool_data.get("type", "api_call"),
        "script_id": tool_data.get("script_id", ""),
        "fields": tool_data.get("fields", {}),
        "required_params": tool_data.get("required_params", []),
        "headers": tool_data.get("headers", {}),
        "static_values": tool_data.get("static_values", {}),
        "function_name": tool_data.get("function_name", ""),
        "status": tool_data.get("status", "active"),
        "created_by": tool_data.get("created_by", "system"),
        "created_at": now,
        "updated_at": now,
    }
    return await store_put("tools", tool["tool_id"], tool)


async def get_tool(tool_id: str) -> dict | None:
    """Get a tool by tool_id."""
    return await store_get("tools", tool_id)


async def list_tools(org_id: str = None) -> list[dict]:
    """List all tools, optionally filtered by org_id."""
    def _filter(t):
        return (not org_id) or t.get("org_id") == org_id
    return await store_list("tools", _filter)


async def update_tool(tool_id: str, updates: dict) -> dict | None:
    """Update a tool's fields."""
    updates["updated_at"] = now_str()
    updates.pop("tool_id", None)
    updates.pop("created_at", None)
    return await store_update("tools", tool_id, updates)


async def delete_tool(tool_id: str) -> bool:
    """Delete a tool and remove it from all agents."""
    deleted = await store_delete("tools", tool_id)
    if deleted:
        all_agents = await store_list("agents")
        for a in all_agents:
            if tool_id in a.get("tools", []):
                a["tools"] = [t for t in a["tools"] if t != tool_id]
                await store_put("agents", a["agent_id"], a)
    return deleted


async def get_tools_by_ids(tool_ids: list[str]) -> list[dict]:
    """Get multiple tools by their IDs (active only)."""
    if not tool_ids:
        return []
    def _filter(t):
        return t.get("tool_id") in tool_ids and t.get("status") == "active"
    return await store_list("tools", _filter)
