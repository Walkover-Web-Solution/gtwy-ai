from globals import BadRequestException, logger
from src.configs.constant import alert_types
from src.db_services.webhook_alert_Dbservice import get_webhook_data
from src.services.proxy.Proxyservice import get_user_org_mapping
from src.services.utils.helper import Helper
from src.utils.alert_utils import (
    DEFAULT_ALERT_TYPES,
    DEFAULT_WEBHOOK_URL,
    build_base_payload,
    build_webhook_payload,
    get_details_payload,
    send_external_alert,
    send_internal_alert,
    should_skip_alert,
)


async def send_alert(
    bridge_id=None,
    org_id=None,
    error_log=None,
    error_type=None,
    bridge_name=None,
    is_embed=None,
    user_id=None,
    thread_id=None,
    service=None,
    response=None,
    user_question=None,
    variables=None,
    api_collection=None,
    is_playground=None,
    is_external_error=False,
    error_location=None,
):
    """Send alerts to configured webhooks.
    
    Args:
        bridge_id: Agent/Bridge identifier
        org_id: Organization ID
        error_log: Error details dictionary (must include error, message, message_id, org_name, configuration)
        error_type: Type of alert (use alert_types constants)
        is_external_error: True for external AI service errors, False for internal errors
        error_location: Error location details (file, function, code, location_string)
    """
    try:
        api_collection = api_collection or {}
        api_name = api_collection.get(service, {}).get("name", None)

        # Internal errors: Send directly to default webhook
        if not is_external_error:
            payload = build_base_payload(bridge_id, org_id, bridge_name, error_type, api_name, is_playground, error_log)
            await send_internal_alert(payload, error_location)
            return

        # External errors: Process through webhook configurations
        result = await get_webhook_data(org_id)
        if not result or "webhook_data" not in result:
            raise BadRequestException("Webhook data is missing in the response.")

        webhook_data = result["webhook_data"]
        
        # Add default alert configuration
        webhook_data.append({
            "org_id": org_id,
            "name": "default alert",
            "webhookConfiguration": {"url": DEFAULT_WEBHOOK_URL, "headers": {}},
            "alertType": DEFAULT_ALERT_TYPES,
            "bridges": ["all"],
        })

        # Prepare context and data source
        data_source = response if error_type == alert_types["broadcast_response"] else error_log
        context = {
            "api_name": api_name,
            "source": "playground" if is_playground else "api",
            "service": service,
        }

        # Get appropriate payload based on error type
        details_payload = get_details_payload(error_type, data_source, context)

        # Send to all matching webhook configurations
        for entry in webhook_data:
            webhook_config = entry.get("webhookConfiguration")
            bridges = entry.get("bridges", [])

            # Check if this webhook should receive this alert
            if error_type not in entry.get("alertType", []):
                continue
            if bridge_id not in bridges and "all" not in bridges:
                continue

            # Skip if metric limit already reached
            comparison_value = response if error_type == alert_types["broadcast_response"] else error_log
            if should_skip_alert(error_type, entry, comparison_value):
                continue

            # Build webhook payload
            payload = build_webhook_payload(
                details_payload, error_type, bridge_id, org_id, user_id, 
                thread_id, service, is_playground, api_name, bridge_name, is_embed
            )

            # Add embed user ID if available
            if user_id and is_embed:
                userinfo = await get_user_org_mapping(user_id, org_id)
                embed_user_id = Helper.extract_embed_user_id(userinfo, org_id)
                if embed_user_id:
                    payload["embeduserId"] = embed_user_id

            # Send alert
            webhook_url = webhook_config["url"]
            headers = webhook_config.get("headers", {})
            await send_external_alert(webhook_url, headers, error_type, payload, response, user_question, variables)

    except Exception as error:
        logger.error(f"Error in send_alert: {str(error)}")
