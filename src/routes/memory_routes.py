from fastapi import APIRouter, Depends, Query, Request

from src.controllers.memory_controller import (
    get_memory_handler,
    list_memories_handler,
    reject_memory_handler,
    verify_memory_handler,
)
from src.middlewares.middleware import jwt_middleware

router = APIRouter()


@router.get("/", dependencies=[Depends(jwt_middleware)])
async def list_memories(
    request: Request,
    agent_id: str | None = Query(default=None, description="Filter by agent/bridge ID"),
    status: str | None = Query(default=None, description="Filter by status: unverified | verified | rejected"),
):
    return await list_memories_handler(agent_id=agent_id, status=status)


@router.get("/{memory_id}", dependencies=[Depends(jwt_middleware)])
async def get_memory(request: Request, memory_id: str):
    return await get_memory_handler(memory_id=memory_id)


@router.post("/{memory_id}/verify", dependencies=[Depends(jwt_middleware)])
async def verify_memory(request: Request, memory_id: str):
    return await verify_memory_handler(memory_id=memory_id)


@router.delete("/{memory_id}", dependencies=[Depends(jwt_middleware)])
async def reject_memory(request: Request, memory_id: str):
    return await reject_memory_handler(memory_id=memory_id)
