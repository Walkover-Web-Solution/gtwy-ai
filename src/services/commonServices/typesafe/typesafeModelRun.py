"""Runner for the TypeSafe (Jev) System One endpoint.

Jev is not a chat model: the request is ``{model, state, questions}`` and the
response is ``{model, answers, usage}``. There is no SDK dependency; the call is
a single ``POST {base_url}/systemone`` made with httpx.
"""

import httpx

from src.configs.service_registry import base_url as service_base_url
from src.exceptions import ApiCallError

from ..api_executor import execute_api_call

TYPESAFE_TIMEOUT_SECONDS = 60.0


def _extract_error_message(response: httpx.Response) -> str:
    """TypeSafe errors look like ``{"detail": {"error_type": ..., "message": ...}}``."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("message") or detail.get("error_type") or str(detail)
    if isinstance(detail, (str, list)):
        return str(detail)
    return str(body)


async def typesafe_runmodel(
    configuration,
    api_key,
    execution_time_logs,
    bridge_id,
    timer,
    message_id,
    org_id,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
):
    async def api_call(config):
        url = f"{(service_base_url(service) or 'https://api.typesafe.ai/v1').rstrip('/')}/systemone"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=TYPESAFE_TIMEOUT_SECONDS) as client:
                http_response = await client.post(url, headers=headers, json=config)
        except httpx.HTTPError as error:
            return {"success": False, "error": f"TypeSafe request failed: {error}", "status_code": 503}

        if http_response.status_code >= 400:
            return {
                "success": False,
                "error": _extract_error_message(http_response),
                "status_code": http_response.status_code,
            }

        try:
            payload = http_response.json()
        except ValueError:
            return {"success": False, "error": "TypeSafe returned a non-JSON response", "status_code": 502}

        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            return {"success": False, "error": "TypeSafe response has no answers", "status_code": 502}

        request_id = http_response.headers.get("x-request-id") or http_response.headers.get("request-id")
        if request_id and not payload.get("request_id"):
            payload["request_id"] = request_id
        return {"success": True, "response": payload}

    try:
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=False,
            name=name,
            org_name=org_name,
            service=service,
            count=count,
            token_calculator=token_calculator,
        )
    except Exception as error:
        raise ApiCallError(str(error), status_code=getattr(error, "status_code", None), service=service) from error
