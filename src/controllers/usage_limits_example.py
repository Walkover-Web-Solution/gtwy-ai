"""
Example controller showing how to integrate usage limits into your LLM call handlers.
This demonstrates the complete flow: check limits → call LLM → settle cost → publish event.
"""

from fastapi import Request, status
from fastapi.responses import JSONResponse

from globals import logger
from src.services.usage_limits_service import usage_limits_service
from src.services.usage_events_producer import publish_usage_event
from src.utils.token_cost_calculator import estimate_cost, calculate_actual_cost


async def call_openai_with_limits(request: Request, prompt: str, model: str = "gpt-4"):
    """
    Example: Call OpenAI with usage limits enforcement.

    Flow:
    1. Check if request is allowed (already done by middleware)
    2. Estimate worst-case cost
    3. Call OpenAI
    4. Calculate actual cost
    5. Settle the difference
    6. Publish usage event
    """

    try:
        org_id = request.state.usage_limits["org_id"]
        bridge_id = request.state.usage_limits["bridge_id"]
        folder_id = request.state.usage_limits["folder_id"]
        apikey_id = request.state.usage_limits["apikey_id"]
        request_id = request.state.request_id

        import openai

        openai.api_key = "your-api-key"

        tokens_in = len(prompt.split())

        response = await openai.ChatCompletion.acreate(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )

        tokens_out = response.usage.completion_tokens
        tokens_in = response.usage.prompt_tokens

        actual_cost = calculate_actual_cost(
            service="openai",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        reservation_cost = request.state.usage_limits["estimated_cost"]

        await usage_limits_service.settle_usage(
            org_id=org_id,
            bridge_id=bridge_id,
            folder_id=folder_id,
            apikey_id=apikey_id,
            reservation_cost=reservation_cost,
            actual_cost=actual_cost,
        )

        await publish_usage_event(
            request_id=request_id,
            org_id=org_id,
            bridge_id=bridge_id,
            service="openai",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=actual_cost,
            status="success",
            folder_id=folder_id,
            apikey_id=apikey_id,
            reservation_cost=reservation_cost,
            actual_cost=actual_cost,
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "response": response.choices[0].message.content,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost": actual_cost,
                "request_id": request_id,
            },
        )

    except Exception as e:
        logger.error(f"Error in call_openai_with_limits: {str(e)}")

        await publish_usage_event(
            request_id=request.state.request_id,
            org_id=request.state.usage_limits["org_id"],
            bridge_id=request.state.usage_limits["bridge_id"],
            service="openai",
            model=model,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            status="error",
            folder_id=request.state.usage_limits.get("folder_id"),
            apikey_id=request.state.usage_limits.get("apikey_id"),
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(e)},
        )


async def get_usage_status(request: Request) -> JSONResponse:
    """
    Get current usage status for a bridge/folder/apikey.
    Useful for dashboards and user-facing status pages.
    """

    try:
        org_id = request.headers.get("X-Org-ID")
        bridge_id = request.headers.get("X-Bridge-ID")
        folder_id = request.headers.get("X-Folder-ID")
        apikey_id = request.headers.get("X-API-Key-ID")

        if not org_id or not bridge_id:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "Missing X-Org-ID or X-Bridge-ID"},
            )

        usage = await usage_limits_service.get_current_usage(
            org_id=org_id,
            bridge_id=bridge_id,
            folder_id=folder_id,
            apikey_id=apikey_id,
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "org_id": org_id,
                "bridge_id": bridge_id,
                "usage": usage,
            },
        )

    except Exception as e:
        logger.error(f"Error in get_usage_status: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(e)},
        )


async def handle_llm_error_with_limits(
    request: Request, error: Exception, model: str
) -> None:
    """
    Handle LLM call errors while recording usage.
    Refunds the reservation if the call failed.
    """

    try:
        org_id = request.state.usage_limits["org_id"]
        bridge_id = request.state.usage_limits["bridge_id"]
        folder_id = request.state.usage_limits["folder_id"]
        apikey_id = request.state.usage_limits["apikey_id"]
        request_id = request.state.request_id
        reservation_cost = request.state.usage_limits["estimated_cost"]

        await usage_limits_service.settle_usage(
            org_id=org_id,
            bridge_id=bridge_id,
            folder_id=folder_id,
            apikey_id=apikey_id,
            reservation_cost=reservation_cost,
            actual_cost=0,
        )

        await publish_usage_event(
            request_id=request_id,
            org_id=org_id,
            bridge_id=bridge_id,
            service="unknown",
            model=model,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            status="error",
            folder_id=folder_id,
            apikey_id=apikey_id,
            reservation_cost=reservation_cost,
            actual_cost=0,
        )

        logger.info(
            f"Refunded reservation of ${reservation_cost} for failed request {request_id}"
        )

    except Exception as e:
        logger.error(f"Error handling LLM error: {str(e)}")
