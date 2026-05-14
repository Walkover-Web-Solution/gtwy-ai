from fastapi import HTTPException
from fastapi.responses import JSONResponse

from globals import logger
from src.services.memory.memory_db_service import (
    delete_memory,
    get_memory,
    list_memories,
    set_memory_status,
)
from src.services.memory.memory_vector_service import delete_memory_from_index, index_memory


async def list_memories_handler(agent_id: str | None, status: str | None) -> JSONResponse:
    if status and status not in ("unverified", "verified", "rejected"):
        raise HTTPException(status_code=400, detail="status must be one of: unverified, verified, rejected")

    docs = await list_memories(agent_id=agent_id, status=status)
    return JSONResponse(status_code=200, content={"success": True, "data": docs, "count": len(docs)})


async def get_memory_handler(memory_id: str) -> JSONResponse:
    doc = await get_memory(memory_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
    return JSONResponse(status_code=200, content={"success": True, "data": doc})


async def verify_memory_handler(memory_id: str) -> JSONResponse:
    doc = await get_memory(memory_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

    if doc.get("status") == "verified":
        return JSONResponse(status_code=200, content={"success": True, "message": "Memory already verified"})

    updated = await set_memory_status(memory_id, "verified")
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update memory status")

    # Only after DB update succeeds do we write to the vector index
    indexed = await index_memory(
        memory_id=memory_id,
        agent_id=doc["agent_id"],
        name=doc.get("name", ""),
        description=doc.get("description", ""),
    )
    if not indexed:
        logger.warning(f"Memory {memory_id} verified in DB but vector indexing failed — search will not find it")

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "memory_id": memory_id,
            "vector_indexed": indexed,
            "message": "Memory verified and indexed for search." if indexed else "Memory verified. Vector indexing failed — check logs.",
        },
    )


async def reject_memory_handler(memory_id: str) -> JSONResponse:
    doc = await get_memory(memory_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

    # Remove from vector index if it was previously verified
    if doc.get("status") == "verified":
        await delete_memory_from_index(memory_id=memory_id, agent_id=doc["agent_id"])

    deleted = await delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete memory")

    return JSONResponse(
        status_code=200,
        content={"success": True, "memory_id": memory_id, "message": "Memory rejected and deleted."},
    )
