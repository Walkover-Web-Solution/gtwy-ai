from config import Config
from globals import BadRequestException, logger
from src.configs.constant import alert_types
from src.configs.notification_events import LEGACY_ALERT_TYPE_TO_EVENT
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
)


async def send_alert(
    bridge_id=None,
    org_id=None,
    error_log=None,
    error_type=None,
    bridge_name=None,
    org_name=None,
    is_embed=None,
    user_id=None,
    thread_id=None,
    service=None,
    response=None,
    user_question=None,
    variables=None,
    api_collection=None,
    is_external_error=False,
    error_location=None,
    webhook_url=None,
):
    try:
        api_collection = api_collection or {}
        api_name = api_collection.get(service, {}).get("name", None)

        # Internal errors: Send directly to default webhook
        if not is_external_error:
            payload = build_base_payload(bridge_id, org_id, bridge_name, org_name, error_type, api_name, error_log, service)
            await send_internal_alert(payload, error_location, webhook_url=webhook_url)
            return

        # External errors: Process through webhook configurations
        data_source = response if error_type == alert_types["broadcast_response"] else error_log
        context = {
            "api_name": api_name,
            "source": "api",
            "service": service,
        }
        details_payload = get_details_payload(error_type, data_source, context)

        # The body is the same for every webhook, so build it once.
        payload = build_webhook_payload(
            details_payload, error_type, bridge_id, org_id, org_name, user_id,
            thread_id, service, api_name, bridge_name, is_embed
        )
        if user_id and is_embed:
            userinfo = await get_user_org_mapping(user_id, org_id)
            embed_user_id = Helper.extract_embed_user_id(userinfo, org_id)
            if embed_user_id:
                payload["embeduserId"] = embed_user_id

        # Notification hub: shadow = also emit (legacy path still sends), on = hub sends instead.
        hub_mode = Config.NOTIFICATION_HUB_MODE
        if hub_mode in ("shadow", "on"):
            await _emit_alert_event(error_type, org_id, bridge_id, bridge_name, details_payload, payload, user_id, thread_id)
        if hub_mode == "on":
            return

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

        # Send to all matching webhook configurations
        for entry in webhook_data:
            webhook_config = entry.get("webhookConfiguration")
            bridges = entry.get("bridges", [])

            # Check if webhook configuration exists
            if not webhook_config:
                continue

            # Check if this webhook should receive this alert
            if error_type not in entry.get("alertType", []):
                continue
            if bridge_id not in bridges and "all" not in bridges:
                continue

            # Send alert
            webhook_url = webhook_config.get("url")
            if not webhook_url:
                continue
            headers = webhook_config.get("headers", {})
            await send_external_alert(webhook_url, headers, error_type, payload, response, user_question, variables)

    except Exception as error:
        logger.error(f"Error in send_alert: {str(error)}")


_ALERT_TITLES = {
    "agent.error": "Agent run failed",
    "agent.variables_missing": "Variables missing",
    "agent.retry_started": "Retry started",
    "agent.response_broadcast": "Agent response broadcast",
    "agent.metrics_limit_reached": "Metrics limit reached",
}


def _alert_message(event_type, details_payload, bridge_name):
    if event_type == "agent.variables_missing":
        variables = details_payload.get("variables")
        names = ", ".join(variables) if isinstance(variables, list | dict) else str(variables or "")
        text = f"Missing variables: {names}" if names else "Required variables were missing"
    else:
        text = str(details_payload.get("error_message") or details_payload.get("alert") or "")
    text = text[:500]
    return f"{bridge_name}: {text}" if bridge_name else text


async def _emit_alert_event(error_type, org_id, bridge_id, bridge_name, details_payload, payload, user_id, thread_id):
    # Lazy import: emit -> queue -> baseQueue imports send_alert lazily too (circular).
    from src.services.notifications.emit import emit_event

    event_type = LEGACY_ALERT_TYPE_TO_EVENT.get(error_type)
    if not event_type:
        return
    await emit_event(
        event_type,
        org_id,
        agent_id=bridge_id,
        title=_ALERT_TITLES[event_type],
        message=_alert_message(event_type, details_payload, bridge_name),
        data={
            "thread_id": thread_id,
            "user_id": user_id,
            # Exactly what the legacy path posts (sendResponse with success=False).
            "webhook_body": {"error": payload, "success": False, "variables": {}},
        },
    )
