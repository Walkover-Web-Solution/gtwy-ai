from fastapi import Request
from fastapi.responses import JSONResponse
from functools import wraps
import traceback
import sys
import json
from src.services.utils.send_error_webhook import send_error_to_webhook
import asyncio

def handle_exceptions(func):
    @wraps(func)
    async def wrapper(request_body, *args, **kwargs):
        try:
            body = request_body.get('body', {})
            return await func(request_body, *args, **kwargs)
        
        except Exception as exc:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            path_params = request_body.get("path_params", {})
            state = request_body.get("state", {})
            is_playground = state.get('is_playground')
            tb = traceback.extract_tb(exc_tb)
            last_frame = tb[-1] if tb else None
            error_location = f"{last_frame.filename.split('/')[-1]}:{last_frame.lineno}" if last_frame else "unknown location"
            
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
                    error_json = {
                        "error_message": error_details
                    }
            else:
                error_json = {
                    "error_message": str(error_details)
                }
   
            bridge_id = path_params.get('bridge_id') or body.get("bridge_id")
            org_id = state.get('profile', {}).get('org', {}).get('id')
            bridge_name = body.get('name')
            is_embed = body.get('is_embed')
            user_id = body.get('user_id')
            thread_id = body.get('thread_id')          
            service = body.get('service')
            await send_error_to_webhook(bridge_id, org_id, error_json, error_type='Error', bridge_name=bridge_name, is_embed=is_embed, user_id=user_id, thread_id=thread_id, service=service)
            return JSONResponse(
                status_code=400,
                content=json.loads(json.dumps(error_json))
            )
    
    return wrapper

