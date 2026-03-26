from fastapi import APIRouter, Depends, File, Request, UploadFile

from ..controllers.image_process_controller import file_processing, image_processing
from ..middlewares.middleware import jwt_middleware

router = APIRouter()


@router.post("/")
async def image(image: UploadFile = File(...)):
    return await image_processing(image)


@router.post("/upload", dependencies=[Depends(jwt_middleware)])
async def upload(request: Request):
    return await file_processing(request)
