from globals import BadRequestException, logger
from src.services.proxy.Proxyservice import get_user_org_mapping

from ...db_services.webhook_alert_Dbservice import get_webhook_data
from ..commonServices.baseService.baseService import sendResponse
from .helper import Helper


async def send_error_to_webhook(
    bridge_id,
    org_id,
    error_log=None,  # Keep for backward compatibility with existing error calls
    error_type=None,
    bridge_name=None,
    is_embed=None,
    user_id=None,
    thread_id=None,
    service=None,
    response=None,  # New parameter for broadcast_response
    user_question=None,  # New parameter for broadcast_response
    variables=None,  # New parameter for broadcast_response
):
    """
    Sends error logs or broadcast responses to a webhook if the specified conditions are met.

    Args:
        bridge_id (str): Identifier for the bridge.
        org_id (str): Identifier for the organization.
        error_log (dict/str): Error log details (backward compatibility).
        error_type (str): Type of the alert (e.g., 'Variable', 'Error', 'metrix_limit_reached', 'broadcast_response').
        response (dict): Response data for broadcast_response type.
        user_question (str): User's question/input.
        variables (dict): Variables associated with the request.

    Returns:
        None
    """
    try:
        # Fetch webhook data for the organization
        result = await get_webhook_data(org_id)
        if not result or "webhook_data" not in result:
            raise BadRequestException("Webhook data is missing in the response.")

        webhook_data = result["webhook_data"]

        # Add default alert configuration if necessary
        webhook_data.append(
            {
                "org_id": org_id,
                "name": "default alert",
                "webhookConfiguration": {"url": "https://flow.sokt.io/func/scriSmH2QaBH", "headers": {}},
                "alertType": ["Error", "Variable", "retry_mechanism"],
                "bridges": ["all"],
            }
        )

        # Generate the appropriate payload based on the error type
        # Use response parameter for broadcast_response, error_log for other types
        data_source = response if error_type == "broadcast_response" else error_log
        
        if error_type == "Variable":
            details_payload = create_missing_vars(data_source if data_source else {})
        elif error_type == "metrix_limit_reached":
            details_payload = metrix_limit_reached(data_source if data_source else 0)
        elif error_type == "retry_mechanism":
            details_payload = create_retry_mechanism_payload(data_source if data_source else "")
        elif error_type == "broadcast_response":
            details_payload = create_broadcast_response_payload(data_source if data_source else {})
        else:
            details_payload = create_error_payload(data_source if data_source else {})

        # Iterate through webhook configurations and send responses
        for entry in webhook_data:
            webhook_config = entry.get("webhookConfiguration")
            bridges = entry.get("bridges", [])

            if error_type in entry.get("alertType", []) and (bridge_id in bridges or "all" in bridges):
                # Use the appropriate data source for comparison
                comparison_value = response if error_type == "broadcast_response" else error_log
                if error_type == "metrix_limit_reached" and entry.get("limit", 500) == (comparison_value if comparison_value else 0):
                    continue
                
                # Use user_url if present, otherwise use url from webhookConfiguration
                webhook_url = entry.get("user_url") or webhook_config["url"]
                headers = webhook_config.get("headers", {})

                # Prepare details for the webhook
                payload = {
                    "details": details_payload,  # Use details_payload directly to avoid nesting
                    "bridge_id": bridge_id,
                    "org_id": org_id,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "service": service,
                }

                # Add bridge_name and is_embed to payload if available
                if bridge_name is not None:
                    payload["bridge_name"] = bridge_name
                if is_embed is not None:
                    payload["is_embed"] = is_embed

                # Fetch user org mapping only if user_id is available
                if user_id and is_embed:
                    userinfo = await get_user_org_mapping(user_id, org_id)
                    embed_user_id = Helper.extract_embed_user_id(userinfo, org_id)
                    if embed_user_id:
                        payload["embeduserId"] = embed_user_id

                # Send the response
                response_format = create_response_format(webhook_url, headers)
                
                # For broadcast_response, send with success=True and include user question and variables
                if error_type == "broadcast_response":
                    broadcast_data = {
                        "response": response if response else {},
                        "user_question": user_question if user_question else "",
                        "variables": variables if variables else {},
                    }
                    await sendResponse(response_format, data=broadcast_data, success=True, variables=variables if variables else {})
                else:
                    await sendResponse(response_format, data=payload)

    except Exception as error:
        logger.error(f"Error in send_error_to_webhook: %s, {str(error)}")


def create_missing_vars(details):
    return {"alert": "variables missing", "Variables": details}


def metrix_limit_reached(details):
    return {"alert": "limit_reached", "Limit Size": details}


def create_error_payload(details):
    return {"alert": "Unexpected Error", "error_message": details["error"]}


def create_retry_mechanism_payload(details):
    return {"alert": "Retry Mechanism Started due to error.", "error_message": details}


def create_broadcast_response_payload(details):
    return {"alert": "Broadcast Response", "response": details}


def create_broadcast_response_payload(details):
    return {"alert": "Broadcast Response", "response": details}


def create_response_format(url, headers):
    return {"type": "webhook", "cred": {"url": url, "headers": headers}}
