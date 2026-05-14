import logging
from typing import Optional

from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from globals import logger
from src.services.usage_limits_service import usage_limits_service

EXCLUDED_PATHS = {"/health", "/metrics", "/docs", "/openapi.json"}


class UsageLimitsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce usage limits on API calls.
    Checks limits before processing the request.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if any(path.startswith(excluded) for excluded in EXCLUDED_PATHS):
            return await call_next(request)

        org_id = request.headers.get("X-Org-ID")
        bridge_id = request.headers.get("X-Bridge-ID")

        if not org_id or not bridge_id:
            logger.warning(
                f"Missing required headers: org_id={org_id}, bridge_id={bridge_id}"
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "Missing X-Org-ID or X-Bridge-ID header"},
            )

        folder_id = request.headers.get("X-Folder-ID")
        apikey_id = request.headers.get("X-API-Key-ID")

        estimated_cost = request.headers.get("X-Estimated-Cost", "0")
        try:
            estimated_cost = float(estimated_cost)
        except ValueError:
            estimated_cost = 0.0

        check_result = await usage_limits_service.check_and_reserve(
            org_id=org_id,
            bridge_id=bridge_id,
            folder_id=folder_id,
            apikey_id=apikey_id,
            estimated_cost=estimated_cost,
        )

        if not check_result["allowed"]:
            logger.warning(
                f"Usage limit exceeded for org={org_id}, bridge={bridge_id}: "
                f"{check_result['reason']}"
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": check_result["reason"],
                    "limit_type": check_result["limit_type"],
                    "current_usage": check_result["current_usage"],
                    "limit_value": check_result["limit_value"],
                },
            )

        request.state.usage_limits = {
            "org_id": org_id,
            "bridge_id": bridge_id,
            "folder_id": folder_id,
            "apikey_id": apikey_id,
            "estimated_cost": estimated_cost,
        }

        response = await call_next(request)

        return response
