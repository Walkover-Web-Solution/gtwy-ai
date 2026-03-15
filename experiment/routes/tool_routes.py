from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.tool_db_service import create_tool, delete_tool, get_tool, list_tools, update_tool
from schemas.tool_schemas import CreateToolRequest, UpdateToolRequest
from services.tool_registry import _axios_work

router = APIRouter(prefix="/tools", tags=["Tools"])


class TestToolRequest(BaseModel):
    args: dict = {}


@router.post("")
async def create_tool_endpoint(request: CreateToolRequest):
    """Create a new tool definition."""
    try:
        tool_data = request.model_dump()
        tool = await create_tool(tool_data)
        return {"success": True, "data": tool}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_tools_endpoint(org_id: str = None):
    """List all tools, optionally filtered by org_id."""
    try:
        tools = await list_tools(org_id)
        return {"success": True, "data": tools, "count": len(tools)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{tool_id}")
async def get_tool_endpoint(tool_id: str):
    """Get a specific tool by ID."""
    tool = await get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")
    return {"success": True, "data": tool}


@router.put("/{tool_id}")
async def update_tool_endpoint(tool_id: str, request: UpdateToolRequest):
    """Update a tool's configuration."""
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    tool = await update_tool(tool_id, updates)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")
    return {"success": True, "data": tool}


@router.delete("/{tool_id}")
async def delete_tool_endpoint(tool_id: str):
    """Delete a tool."""
    deleted = await delete_tool(tool_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")
    return {"success": True, "message": f"Tool '{tool_id}' deleted."}


@router.post("/{tool_id}/test")
async def test_tool_endpoint(tool_id: str, request: TestToolRequest):
    """Run a tool directly with given args and return the raw response. For testing only."""
    tool = await get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")

    if tool.get("type") != "api_call":
        raise HTTPException(status_code=400, detail="Only api_call tools can be tested via this endpoint.")

    script_id = tool.get("script_id")
    if not script_id:
        raise HTTPException(status_code=400, detail="Tool has no script_id configured.")

    url = f"https://flow.sokt.io/func/{script_id}"
    headers = tool.get("headers") or {}

    result = await _axios_work(request.args, url, headers)
    return {
        "success": result.get("status") == 1,
        "tool_id": tool_id,
        "tool_name": tool.get("name"),
        "script_id": script_id,
        "url": url,
        "args_sent": request.args,
        "response": result.get("response"),
        "status": result.get("status"),
    }
