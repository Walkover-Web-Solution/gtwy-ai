import traceback
import os
from ..api_executor import execute_api_call
from ...utils.apiservice import fetch
from globals import *

async def grok_runmodel(configuration, api_key, execution_time_logs, bridge_id, timer, message_id, org_id, name="", org_name="", service="", count=0, token_calculator=None):
    """Execute a chat completion call against the xAI Grok API using custom fetch function."""

    async def api_call(config):
        try:
            # Prepare the request payload for xAI API
            url = "https://api.x.ai/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            # Use the custom fetch function to make the API call
            response_data, response_headers = await fetch(
                url=url,
                method="POST",
                headers=headers,
                json_body=config
            )
            
            # Parse the response similar to OpenAI format
            return {
                "success": True,
                "response": response_data
            }
        except Exception as error:
            return {
                "success": False,
                "error": str(error),
                "status_code": getattr(error, 'status_code', None)
            }

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
            token_calculator=token_calculator
        )
    except Exception as error:
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})
        logger.error("Grok runmodel error=>", error)
        traceback.print_exc()
        return {
            "success": False,
            "error": str(error)
        }

