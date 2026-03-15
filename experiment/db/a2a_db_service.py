from db.json_store import now_str, store_delete, store_get, store_list, store_put, store_update


async def register_agent_card(agent_id: str, agent_card: dict, org_id: str = "default") -> dict:
    """Register or update an agent's A2A card in the registry."""
    now = now_str()
    existing = await store_get("a2a_registry", agent_id)
    entry = {
        "agent_id": agent_id,
        "org_id": org_id,
        "agent_card": agent_card,
        "discoverable": agent_card.get("discoverable", True),
        "allowed_callers": agent_card.get("allowed_callers", []),
        "status": "active",
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
    }
    return await store_put("a2a_registry", agent_id, entry)


async def get_agent_card(agent_id: str) -> dict | None:
    """Get an agent's A2A card from the registry."""
    return await store_get("a2a_registry", agent_id)


async def discover_agents(org_id: str = None) -> list[dict]:
    """Discover all available agents in the A2A registry."""
    def _filter(e):
        if e.get("status") != "active" or not e.get("discoverable", True):
            return False
        return (not org_id) or e.get("org_id") == org_id
    return await store_list("a2a_registry", _filter)


async def update_agent_card(agent_id: str, updates: dict) -> dict | None:
    """Update an agent's A2A card."""
    updates["updated_at"] = now_str()
    return await store_update("a2a_registry", agent_id, updates)


async def remove_agent_card(agent_id: str) -> bool:
    """Remove an agent from the A2A registry."""
    return await store_delete("a2a_registry", agent_id)


async def check_caller_permission(agent_id: str, caller_agent_id: str) -> bool:
    """Check if a caller agent is allowed to invoke the target agent via A2A."""
    entry = await store_get("a2a_registry", agent_id)
    if not entry or entry.get("status") != "active":
        return False
    allowed_callers = entry.get("allowed_callers", [])
    if not allowed_callers:
        return True
    return caller_agent_id in allowed_callers
