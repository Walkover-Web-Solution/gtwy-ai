from fastapi import APIRouter, HTTPException

from db.a2a_db_service import get_agent_card, remove_agent_card, update_agent_card
from db.agent_db_service import add_sub_agent, get_agent, remove_sub_agent
from schemas.a2a_schemas import A2AInvokeRequest, AgentCardRequest, LinkSubAgentRequest
from services.a2a_service import build_agent_card, discover_available_agents, invoke_agent_a2a

router = APIRouter(prefix="/a2a", tags=["Agent-to-Agent"])


@router.get("/discover")
async def discover_agents_endpoint(org_id: str = None):
    """Discover all agents available for A2A communication."""
    try:
        agents = await discover_available_agents(org_id)
        return {"success": True, "data": agents, "count": len(agents)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{agent_id}/card")
async def get_agent_card_endpoint(agent_id: str):
    """Get an agent's A2A card."""
    card = await get_agent_card(agent_id)
    if not card:
        # Try to auto-build it
        card = await build_agent_card(agent_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"Agent card for '{agent_id}' not found.")
    return {"success": True, "data": card}


@router.put("/agents/{agent_id}/card")
async def update_agent_card_endpoint(agent_id: str, request: AgentCardRequest):
    """Update an agent's A2A card settings."""
    updates = {k: v for k, v in request.model_dump().items() if v is not None}

    # Ensure agent exists
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    # Build card first if it doesn't exist
    existing = await get_agent_card(agent_id)
    if not existing:
        await build_agent_card(agent_id)

    result = await update_agent_card(agent_id, updates)
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update agent card.")
    return {"success": True, "data": result}


@router.delete("/agents/{agent_id}/card")
async def remove_agent_card_endpoint(agent_id: str):
    """Remove an agent from the A2A registry."""
    deleted = await remove_agent_card(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent card for '{agent_id}' not found.")
    return {"success": True, "message": f"Agent card for '{agent_id}' removed."}


@router.post("/agents/{agent_id}/invoke")
async def a2a_invoke_endpoint(agent_id: str, request: A2AInvokeRequest):
    """Invoke an agent via A2A protocol."""
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    result = await invoke_agent_a2a(
        target_agent_id=agent_id,
        input_text=request.input_text,
        caller_agent_id=request.caller_agent_id,
        api_key=request.api_key,
    )

    if result.startswith("A2A Error:"):
        raise HTTPException(status_code=400, detail=result)

    return {"success": True, "data": {"agent_id": agent_id, "response": result}}


@router.post("/agents/{agent_id}/sub-agents")
async def link_sub_agent_endpoint(agent_id: str, request: LinkSubAgentRequest):
    """Link a sub-agent to a parent agent for A2A delegation."""
    # Validate parent agent exists
    parent = await get_agent(agent_id)
    if not parent:
        raise HTTPException(status_code=404, detail=f"Parent agent '{agent_id}' not found.")

    result = await add_sub_agent(agent_id, request.sub_agent_id)
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Failed to link sub-agent. Check that the sub-agent exists and is not the same as the parent.",
        )
    return {"success": True, "data": result}


@router.delete("/agents/{agent_id}/sub-agents/{sub_agent_id}")
async def unlink_sub_agent_endpoint(agent_id: str, sub_agent_id: str):
    """Unlink a sub-agent from a parent agent."""
    result = await remove_sub_agent(agent_id, sub_agent_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return {"success": True, "data": result}
