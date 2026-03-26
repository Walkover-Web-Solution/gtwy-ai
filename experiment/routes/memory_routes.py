from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.memory_db_service import delete_memory, save_memory, search_memories

router = APIRouter(prefix="/memories", tags=["Memories"])


class SaveMemoryRequest(BaseModel):
    content: str
    category: str = "learnings"
    metadata: dict = {}


class SearchMemoriesRequest(BaseModel):
    query: str | None = None
    category: str | None = None
    limit: int = 20


@router.post("/{agent_id}")
async def save_memory_endpoint(agent_id: str, request: SaveMemoryRequest):
    """Save a memory for an agent."""
    try:
        result = await save_memory(
            agent_id=agent_id,
            content=request.content,
            category=request.category,
            metadata=request.metadata,
        )
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}")
async def list_memories_endpoint(
    agent_id: str,
    query: str = None,
    category: str = None,
    limit: int = 20,
):
    """Search/list memories for an agent."""
    try:
        memories = await search_memories(
            agent_id=agent_id,
            query=query,
            category=category,
            limit=limit,
        )
        return {"success": True, "data": memories, "count": len(memories)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{agent_id}/{category}/{key}")
async def delete_memory_endpoint(agent_id: str, category: str, key: str):
    """Delete a specific memory."""
    try:
        await delete_memory(agent_id=agent_id, key=key, category=category)
        return {"success": True, "message": f"Memory '{key}' deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
