import uuid

from db.json_store import now_str, store_delete, store_get, store_list, store_put, store_update


def _generate_agent_id() -> str:
    return f"agent_{uuid.uuid4().hex[:12]}"


async def create_agent(agent_data: dict) -> dict:
    """Create a new agent."""
    now = now_str()
    agent = {
        "agent_id": _generate_agent_id(),
        "org_id": agent_data.get("org_id", "default"),
        "name": agent_data["name"],
        "description": agent_data.get("description", ""),
        "system_prompt": agent_data.get("system_prompt", "You are a helpful AI assistant."),
        "model": agent_data.get("model", "gpt-4o-mini"),
        "planner_model": agent_data.get("planner_model"),
        "executor_model": agent_data.get("executor_model"),
        "temperature": agent_data.get("temperature", 0.3),
        "max_tokens": agent_data.get("max_tokens", 4096),
        "enable_reflection": agent_data.get("enable_reflection", True),
        "tools": agent_data.get("tools", []),
        "sub_agents": agent_data.get("sub_agents", []),
        "pretool": agent_data.get("pretool"),
        "pretool_input": agent_data.get("pretool_input", {}),
        "status": agent_data.get("status", "active"),
        "created_by": agent_data.get("created_by", "system"),
        "created_at": now,
        "updated_at": now,
    }
    return await store_put("agents", agent["agent_id"], agent)


async def get_agent(agent_id: str) -> dict | None:
    """Get an agent by agent_id."""
    return await store_get("agents", agent_id)


async def list_agents(org_id: str = None) -> list[dict]:
    """List all agents, optionally filtered by org_id."""
    def _filter(a):
        return (not org_id) or a.get("org_id") == org_id
    return await store_list("agents", _filter)


async def update_agent(agent_id: str, updates: dict) -> dict | None:
    """Update an agent's fields."""
    updates["updated_at"] = now_str()
    updates.pop("agent_id", None)
    updates.pop("created_at", None)
    return await store_update("agents", agent_id, updates)


async def delete_agent(agent_id: str) -> bool:
    """Delete an agent and clean up references."""
    deleted = await store_delete("agents", agent_id)
    if deleted:
        await store_delete("a2a_registry", agent_id)
        # Remove from all other agents' sub_agents lists
        all_agents = await store_list("agents")
        for a in all_agents:
            if agent_id in a.get("sub_agents", []):
                a["sub_agents"] = [s for s in a["sub_agents"] if s != agent_id]
                await store_put("agents", a["agent_id"], a)
    return deleted


async def add_tool_to_agent(agent_id: str, tool_id: str) -> dict | None:
    """Attach a tool to an agent."""
    agent = await store_get("agents", agent_id)
    if not agent:
        return None
    tools = agent.get("tools", [])
    if tool_id not in tools:
        tools.append(tool_id)
    return await store_update("agents", agent_id, {"tools": tools, "updated_at": now_str()})


async def remove_tool_from_agent(agent_id: str, tool_id: str) -> dict | None:
    """Detach a tool from an agent."""
    agent = await store_get("agents", agent_id)
    if not agent:
        return None
    tools = [t for t in agent.get("tools", []) if t != tool_id]
    return await store_update("agents", agent_id, {"tools": tools, "updated_at": now_str()})


async def add_sub_agent(agent_id: str, sub_agent_id: str) -> dict | None:
    """Link a sub-agent for A2A delegation."""
    if agent_id == sub_agent_id:
        return None
    sub_agent = await store_get("agents", sub_agent_id)
    if not sub_agent:
        return None
    agent = await store_get("agents", agent_id)
    if not agent:
        return None
    subs = agent.get("sub_agents", [])
    if sub_agent_id not in subs:
        subs.append(sub_agent_id)
    return await store_update("agents", agent_id, {"sub_agents": subs, "updated_at": now_str()})


async def remove_sub_agent(agent_id: str, sub_agent_id: str) -> dict | None:
    """Unlink a sub-agent."""
    agent = await store_get("agents", agent_id)
    if not agent:
        return None
    subs = [s for s in agent.get("sub_agents", []) if s != sub_agent_id]
    return await store_update("agents", agent_id, {"sub_agents": subs, "updated_at": now_str()})
