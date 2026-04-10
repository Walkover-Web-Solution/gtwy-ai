from config import Config
from src.configs.constant import alert_types
from src.services.commonServices.baseService.baseService import sendResponse
from src.services.utils.apiservice import fetch
from src.utils.alert_template import (
    create_missing_vars,
    metrix_limit_reached,
    create_error_payload,
    create_retry_mechanism_payload,
    create_broadcast_response_payload,
    create_response_format,
)

DEFAULT_WEBHOOK_URL = "https://flow.sokt.io/func/scriYP8m551q"
DEFAULT_ALERT_TYPES = [alert_types["error"], alert_types["variable"], alert_types["retry_mechanism"]]


def build_base_payload(bridge_id, org_id, bridge_name, error_type, api_name, is_playground, error_log):
    """Build base payload for internal errors.
    
    Args:
        bridge_id: Agent/Bridge identifier
        org_id: Organization ID
        bridge_name: Bridge/Agent name
        error_type: Type of alert
        api_name: API name from collection
        is_playground: Whether request is from playground
        error_log: Error details dictionary
        
    Returns:
        dict: Formatted payload for internal error alerts
    """
    payload = {
        **error_log,
        "agent_id": bridge_id,
        "org_id": org_id,
        "bridge_name": bridge_name,
        "alert_type": error_type,
        "api_name": api_name,
        "is_playground": is_playground if is_playground is not None else False,
    }
    return payload


def get_details_payload(error_type, data_source, context):
    """Get the appropriate details payload based on error type.
    
    Uses strategy pattern to map error types to their payload creators.
    
    Args:
        error_type: Type of alert (from alert_types constants)
        data_source: Source data for the alert
        context: Additional context (api_name, source, service)
        
    Returns:
        dict: Formatted details payload
    """
    payload_map = {
        alert_types["variable"]: lambda: create_missing_vars(data_source or {}, context),
        alert_types["metrix_limit_reached"]: lambda: metrix_limit_reached(data_source or 0, context),
        alert_types["retry_mechanism"]: lambda: create_retry_mechanism_payload(data_source or "", context),
        alert_types["broadcast_response"]: lambda: create_broadcast_response_payload(data_source or {}, context),
    }
    return payload_map.get(error_type, lambda: create_error_payload(data_source or {}, context))()


def build_webhook_payload(details_payload, error_type, bridge_id, org_id, user_id, thread_id, service, is_playground, api_name, bridge_name, is_embed):
    """Build webhook payload with all required fields.
    
    Args:
        details_payload: Detailed error/alert information
        error_type: Type of alert
        bridge_id: Agent/Bridge identifier
        org_id: Organization ID
        user_id: User identifier
        thread_id: Thread identifier
        service: Service name
        is_playground: Whether from playground
        api_name: API name
        bridge_name: Bridge name
        is_embed: Whether embedded
        
    Returns:
        dict: Complete webhook payload
    """
    payload = {
        "details": details_payload,
        "alert_type": error_type,
        "agent_id": bridge_id,
        "org_id": org_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "service": service,
        "source": "playground" if is_playground else "api",
    }
    
    # Add optional fields only if they have values
    if api_name is not None:
        payload["api_name"] = api_name
    if bridge_name is not None:
        payload["bridge_name"] = bridge_name
    if is_embed is not None:
        payload["is_embed"] = is_embed
    if is_playground is not None:
        payload["is_playground"] = is_playground
    
    return payload


def should_skip_alert(error_type, entry, comparison_value):
    """Check if alert should be skipped based on limit.
    
    Args:
        error_type: Type of alert
        entry: Webhook configuration entry
        comparison_value: Value to compare against limit
        
    Returns:
        bool: True if alert should be skipped
    """
    return (
        error_type == alert_types["metrix_limit_reached"] and 
        entry.get("limit", 500) == (comparison_value if comparison_value else 0)
    )


async def send_internal_alert(payload, error_location):
    """Send alert for internal errors directly to default webhook.
    
    Args:
        payload: Alert payload
        error_location: Error location details (file, function, code, location_string)
    """
    if error_location:
        payload["error_location"] = error_location
    if Config.ENVIROMENT:
        payload["ENVIROMENT"] = Config.ENVIROMENT
    
    await fetch(DEFAULT_WEBHOOK_URL, method="POST", json_body=payload)


async def send_external_alert(webhook_url, headers, error_type, payload, response, user_question, variables):
    """Send alert for external errors through configured webhooks.
    
    Args:
        webhook_url: Webhook URL to send to
        headers: HTTP headers for webhook request
        error_type: Type of alert
        payload: Alert payload
        response: Response data (for broadcast_response type)
        user_question: User question (for broadcast_response type)
        variables: Variables (for broadcast_response type)
    """
    response_format = create_response_format(webhook_url, headers)
    
    if error_type == alert_types["broadcast_response"]:
        broadcast_data = {
            "response": response or {},
            "user_question": user_question or "",
            "variables": variables or {},
        }
        await sendResponse(response_format, data=broadcast_data, success=True, variables=variables or {})
    else:
        await sendResponse(response_format, data=payload)
