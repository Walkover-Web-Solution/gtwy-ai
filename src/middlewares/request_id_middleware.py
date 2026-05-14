import logging
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_CONTEXT_KEY = "request_id"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that generates and tracks a unique request_id for each request.
    This is used for idempotency and billing reconciliation.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER)

        if not request_id:
            request_id = str(uuid.uuid4())

        request.state.request_id = request_id

        response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id

        logger.debug(f"Request {request.method} {request.url.path} - ID: {request_id}")

        return response
