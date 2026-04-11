import asyncio
import json
import sys
import traceback
from functools import wraps

from fastapi.responses import JSONResponse

from src.configs.constant import alert_types
from src.send_alert import send_alert


def handle_exceptions(func):
    @wraps(func)
    async def wrapper(request_body, *args, **kwargs):
        try:
            body = request_body.get("body", {})
            return await func(request_body, *args, **kwargs)

        except Exception as exc:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            path_params = request_body.get("path_params", {})
            state = request_body.get("state", {})
            state.get("is_playground")

            # Extract error location details
            tb = traceback.extract_tb(exc_tb)
            last_frame = tb[-1] if tb else None
            (f"{last_frame.filename.split('/')[-1]}:{last_frame.lineno}" if last_frame else "unknown location")

            error_location = None
            if last_frame:
                error_location = {
                    "file": last_frame.filename,
                    "function": last_frame.name,
                    "code": last_frame.line or "",
                    "location_string": f"{last_frame.filename.split('/')[-1]}:{last_frame.lineno}"
                }

            if isinstance(exc, ValueError):
                error_details = exc.args[0] if exc.args else str(exc)
            else:
                error_details = str(exc)

            if isinstance(error_details, ValueError):
                error_details = error_details.args[0] if error_details.args else str(error_details)

            if isinstance(error_details, dict):
                error_json = error_details
            elif isinstance(error_details, str):
                try:
                    error_json = json.loads(error_details)
                except json.JSONDecodeError:
                    error_json = {"error_message": error_details}
            else:
                error_json = {"error_message": str(error_details)}               
            body = request_body.get("body", {})
            bridge_id = path_params.get("bridge_id") or body.get("bridge_id")
            org_id = state.get("profile", {}).get("org", {}).get("id")
            org_name = state.get("profile", {}).get("org", {}).get("name")
            bridge_name = body.get("name")
            is_embed = body.get("is_embed")
            user_id = body.get("user_id")
            thread_id = body.get("thread_id")
            service = body.get("service")
            is_playground = state.get("is_playground") or body.get("is_playground") or False
            api_collection = body.get("api_collection")
            asyncio.create_task(send_alert(
                bridge_id=bridge_id,
                org_id=org_id,
                error_log=error_json,
                error_type=alert_types["error"],
                bridge_name=bridge_name,
                org_name=org_name,
                is_embed=is_embed,
                user_id=user_id,
                thread_id=thread_id,
                service=service,
                is_playground=is_playground,
                api_collection=api_collection,
                is_external_error=False,
                error_location=error_location,
            ))
            return JSONResponse(status_code=400, content=json.loads(json.dumps(error_json)))

    return wrapper
