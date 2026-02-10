from fastapi import Request
from .middleware import jwt_middleware
from .ratelimitMiddleware import rate_limit

from src.services.utils.openai_sdk_utils import (
    convert_bearer_to_local_auth,
    build_and_override_request_body
)


async def openai_sdk_middleware(request: Request):
    await build_and_override_request_body(request)
    convert_bearer_to_local_auth(request)

    openai_payload = await request.json()
    request.state.openai_payload = openai_payload

    await jwt_middleware(request)
    await rate_limit(request, key_path="body.bridge_id", points=100)
    await rate_limit(request, key_path="body.thread_id", points=20)