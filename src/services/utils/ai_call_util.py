import json

import jwt

from config import Config
from globals import logger

from .apiservice import fetch


def generate_token(payload, accesskey):
    return jwt.encode(payload, accesskey)


async def call_ai_middleware(user, bridge_id, variables=None, configuration=None, response_type=None, thread_id=None):
    request_body = {"user": user, "bridge_id": bridge_id, "variables": variables}
    if response_type is not None:
        request_body["response_type"] = response_type

    if configuration is not None:
        request_body["configuration"] = configuration

    if thread_id is not None:
        request_body["thread_id"] = thread_id

    response, rs_headers = await fetch(
        "https://api.gtwy.ai/api/v2/model/chat/completion",
        "POST",
        {"pauthkey": Config.AI_MIDDLEWARE_PAUTH_KEY, "Content-Type": "application/json", "Accept-Encoding": "gzip"},
        None,
        request_body,
    )
    if not response.get("success", True):
        raise Exception(response.get("message", "Unknown error"))
    result = response.get("response", {}).get("data", {}).get("content", "")
    if response_type is None:
        result = json.loads(result)
    return result


async def call_gtwy_agent(args):
    # Initialize variables that might be used in exception handler
    message_id = ""
    version_id = args.get("version_id")
    bridge_id = args.get("bridge_id")

    try:
        # Import inside function to avoid circular imports
        from src.services.commonServices.common import chat

        request_body = {}
        # Add thread_id and sub_thread_id if provided
        if args.get("thread_id"):
            request_body["thread_id"] = args.get("thread_id")
        if args.get("sub_thread_id"):
            request_body["sub_thread_id"] = args.get("sub_thread_id")

        org_id = args.get("org_id")
        user_message = args.get("user")
        variables = args.get("variables") or {}

        # Step 1: Update request body with core data
        request_body.update({"user": user_message, "bridge_id": bridge_id})
        # If version_id is provided, include it in the request body early
        if version_id:
            request_body["version_id"] = version_id

        # Step 2: Use pre-fetched bridge_configurations data
        # All agents should have access to bridge_configurations from the parent
        bridge_configurations = args.get("bridge_configurations")

        # Use pre-fetched configuration data
        logger.info(f"Using pre-fetched configuration for agent: {bridge_id}")
        primary_config = bridge_configurations[bridge_id]

        # Step 3: Update request body with configuration data
        request_body.update(primary_config)
        request_body["variables"] = variables
        request_body["org_id"] = org_id
        request_body["bridge_configurations"] = bridge_configurations

        # Step 4: Create data structure for chat function
        # Pass timer state from parent request to maintain latency tracking in recursive calls
        state_data = {}
        state_data["timer"] = args.get("timer_state")

        data_to_send = {"body": request_body, "state": state_data}

        # Step 5: Call the chat function directly
        response = await chat(data_to_send)

        # Handle JSONResponse object - extract the actual response data
        if hasattr(response, "body"):
            # For JSONResponse, get the body content
            import json

            response_data = json.loads(response.body.decode("utf-8"))
        else:
            # If it's already a dict, use it directly
            response_data = response

        if not response_data.get("success", True):
            raise Exception(response_data.get("message", "Unknown error"))

        data_section = response_data.get("response", {}).get("data", {})
        result = data_section.get("content", "")
        message_id = data_section.get("message_id", "")
        resolved_version_id = primary_config.get("version_id", None)

        # Check for image URLs and include them if present
        image_urls = data_section.get("image_urls")

        try:
            parsed_result = json.loads(result) if result else {}
        except json.JSONDecodeError:
            parsed_result = {"data": result}

        # Add image URLs to the result if they exist
        if image_urls:
            if isinstance(parsed_result, dict):
                parsed_result["image_urls"] = image_urls
            else:
                parsed_result = {"data": parsed_result, "image_urls": image_urls}

        return {
            "response": parsed_result,
            "metadata": {
                "agent_id": bridge_id,
                "version_id": resolved_version_id,
                "message_id": message_id,
                "thread_id": args.get("thread_id"),
                "subthread_id": args.get("subthread_id"),
                "type": "agent",
            },
            "status": 1,
        }

    except Exception as e:
        return {
            "response": str(e),
            "metadata": {
                "agent_id": bridge_id,
                "version_id": version_id,
                "message_id": message_id,
                "thread_id": args.get("thread_id"),
                "subthread_id": args.get("subthread_id"),
                "type": "agent",
            },
            "status": 0,
        }


async def get_ai_middleware_agent_data(bridge_id):
    try:
        response, rs_headers = await fetch(
            f"https://api.gtwy.ai/api/v1/config/getbridges/{bridge_id}",
            "GET",
            {"pauthkey": Config.AI_MIDDLEWARE_PAUTH_KEY, "Content-Type": "application/json", "Accept-Encoding": "gzip"},
            None,
            None,
        )

        if not response.get("success", True):
            raise Exception(response.get("message", "Unknown error"))

        return response

    except Exception as e:
        raise Exception(f"Failed to fetch bridge data: {str(e)}") from e
