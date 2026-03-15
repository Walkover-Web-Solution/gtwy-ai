from fastapi import APIRouter, HTTPException

from db.agent_db_service import (
    add_tool_to_agent,
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    remove_tool_from_agent,
    update_agent,
)
from schemas.agent_schemas import CreateAgentRequest, InvokeAgentRequest, UpdateAgentRequest
from services.a2a_service import build_agent_card
from services.agent_service import invoke_agent

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.post("")
async def create_agent_endpoint(request: CreateAgentRequest):
    """Create a new agent."""
    try:
        agent = await create_agent(request.model_dump())

        # Auto-register A2A card for the new agent
        await build_agent_card(agent["agent_id"])

        return {"success": True, "data": agent}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_agents_endpoint(org_id: str = None):
    """List all agents, optionally filtered by org_id."""
    try:
        agents = await list_agents(org_id)
        return {"success": True, "data": agents, "count": len(agents)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}")
async def get_agent_endpoint(agent_id: str):
    """Get a specific agent by ID."""
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return {"success": True, "data": agent}


@router.put("/{agent_id}")
async def update_agent_endpoint(agent_id: str, request: UpdateAgentRequest):
    """Update an agent's configuration."""
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    agent = await update_agent(agent_id, updates)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    # Re-register A2A card if name or description changed
    if "name" in updates or "description" in updates:
        await build_agent_card(agent_id)

    return {"success": True, "data": agent}


@router.delete("/{agent_id}")
async def delete_agent_endpoint(agent_id: str):
    """Delete an agent."""
    deleted = await delete_agent(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return {"success": True, "message": f"Agent '{agent_id}' deleted."}


@router.post("/{agent_id}/invoke")
async def invoke_agent_endpoint(agent_id: str, request: InvokeAgentRequest):
    """Invoke an agent (non-streaming). Runs the full plan-execute-synthesize pipeline."""
    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    result = await invoke_agent(
        agent_id=agent_id,
        goal=request.goal,
        api_key=request.api_key,
        org_id=request.org_id,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return {"success": True, "data": result}


@router.post("/{agent_id}/tools/{tool_id}")
async def attach_tool_endpoint(agent_id: str, tool_id: str):
    """Attach a tool to an agent."""
    result = await add_tool_to_agent(agent_id, tool_id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return {"success": True, "data": result}


@router.delete("/{agent_id}/tools/{tool_id}")
async def detach_tool_endpoint(agent_id: str, tool_id: str):
    """Detach a tool from an agent."""
    result = await remove_tool_from_agent(agent_id, tool_id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return {"success": True, "data": result}
