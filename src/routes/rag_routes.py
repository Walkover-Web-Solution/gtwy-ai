from fastapi import APIRouter, Depends, Request

from ..controllers.rag_controller import create_vectors, delete_doc, get_all_docs, get_vectors_and_text
from ..middlewares.middleware import jwt_middleware

router = APIRouter()


@router.post("/", dependencies=[Depends(jwt_middleware)])
async def create_vertors(request: Request):
    return await create_vectors(request)


@router.post("/query", dependencies=[Depends(jwt_middleware)])
async def get_query(request: Request):
    return await get_vectors_and_text(request)


@router.get("/docs", dependencies=[Depends(jwt_middleware)])
async def get_docs(request: Request):
    return await get_all_docs(request)


@router.delete("/docs", dependencies=[Depends(jwt_middleware)])
async def delete_org_docs(request: Request):
    return await delete_doc(request)
