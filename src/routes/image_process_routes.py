from fastapi import APIRouter, Depends, Request

from ..controllers.image_process_controller import file_processing, image_processing
from ..middlewares.middleware import jwt_middleware

router = APIRouter()


@router.post("/")
async def image(request: Request):
    return await image_processing(request)


@router.post("/upload", dependencies=[Depends(jwt_middleware)])
async def upload(request: Request):
    return await file_processing(request)
