from fastapi import APIRouter, Depends, Request

from ..controllers.provider_files_controller import list_org_files
from ..middlewares.middleware import jwt_middleware

router = APIRouter()


@router.get("/", dependencies=[Depends(jwt_middleware)])
async def get_org_files(request: Request):
    """List the org's provider Files-API uploads (registry rows), newest first.

    Query params: status (uploading|active|deleting|dead_letter), limit (default 50, max 200), skip,
    include_provider_status (true|false, default false) — when true, live-checks each OpenAI file via
    files.retrieve() and adds exists_on_provider/provider_status/provider_bytes per row.
    """
    return await list_org_files(request)
