from fastapi import APIRouter, Depends, Request

from ..controllers.rag_controller import get_vectors_and_text
from ..middlewares.middleware import jwt_middleware
from ..schemas.rag_schemas import RAGQueryRequest

router = APIRouter()


@router.post("/query", dependencies=[Depends(jwt_middleware)])
async def get_query(request: Request, body: RAGQueryRequest):
    return await get_vectors_and_text(body)
