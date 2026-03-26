from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.tool_db_service import create_tool, delete_tool, get_tool, list_tools, update_tool
from schemas.tool_schemas import AddFieldRequest, BulkFieldsRequest, CreateToolRequest, UpdateToolRequest
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


# ── Normal mode: add/update one field at a time ──
@router.post("/{tool_id}/fields")
async def add_field_endpoint(tool_id: str, request: AddFieldRequest):
    """Normal mode — add or update a single field variable on a tool."""
    tool = await get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")

    fields = dict(tool.get("fields") or {})
    required_params = list(tool.get("required_params") or [])

    # Upsert the field
    field_entry = {
        "type": request.type or "string",
        "description": request.description or "",
        "source": request.source or "ai",
    }
    if request.default is not None:
        field_entry["default"] = request.default
    fields[request.field_name] = field_entry

    # Sync required_params
    if request.required and request.field_name not in required_params:
        required_params.append(request.field_name)
    elif not request.required and request.field_name in required_params:
        required_params.remove(request.field_name)

    updated = await update_tool(tool_id, {"fields": fields, "required_params": required_params})
    return {"success": True, "data": updated}


# ── Advance mode: paste full fields JSON at once ──
@router.put("/{tool_id}/fields/bulk")
async def bulk_fields_endpoint(tool_id: str, request: BulkFieldsRequest):
    """Advance mode — replace all fields by pasting a complete JSON object."""
    tool = await get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")

    updates = {"fields": request.fields}
    if request.required_params is not None:
        updates["required_params"] = request.required_params
    if request.static_values is not None:
        updates["static_values"] = request.static_values

    updated = await update_tool(tool_id, updates)
    return {"success": True, "data": updated}


# ── Delete a single field ──
@router.delete("/{tool_id}/fields/{field_name}")
async def delete_field_endpoint(tool_id: str, field_name: str):
    """Remove a single field variable from a tool."""
    tool = await get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found.")

    fields = dict(tool.get("fields") or {})
    if field_name not in fields:
        raise HTTPException(status_code=404, detail=f"Field '{field_name}' not found on tool '{tool_id}'.")

    del fields[field_name]
    required_params = [p for p in (tool.get("required_params") or []) if p != field_name]
    static_values = {k: v for k, v in (tool.get("static_values") or {}).items() if k != field_name}

    updated = await update_tool(tool_id, {"fields": fields, "required_params": required_params, "static_values": static_values})
    return {"success": True, "data": updated}


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

    static_values = tool.get("static_values") or {}
    merged_args = {**static_values, **request.args}

    result = await _axios_work(merged_args, url, headers)
    return {
        "success": result.get("status") == 1,
        "tool_id": tool_id,
        "tool_name": tool.get("name"),
        "script_id": script_id,
        "url": url,
        "args_sent": merged_args,
        "response": result.get("response"),
        "status": result.get("status"),
    }
