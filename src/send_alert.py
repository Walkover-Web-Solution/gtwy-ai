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
    """Queue an alert for the notification hub, which sends it everywhere it belongs: the org's
    in-app notifications, the webhooks set up in the Alerts section and GTWY team alerting.
    Only if the queue is unavailable is the alert sent directly, as before.
    """
    try:
        api_collection = api_collection or {}
        api_name = api_collection.get(service, {}).get("name", None)

        # Internal errors: for the GTWY team (and, when tied to an org, shown to the org too)
        if not is_external_error:
            payload = build_base_payload(bridge_id, org_id, bridge_name, org_name, error_type, api_name, error_log, service)
            # An explicit webhook_url is the RabbitMQ-failure alert: never route it through the queue.
            if webhook_url or not await _queue_internal_error(payload, error_location, org_id, bridge_id, bridge_name, error_type, service):
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

        if await _queue_alert(error_type, org_id, bridge_id, bridge_name, details_payload, payload, user_id, thread_id, service):
            return

        # Queue unavailable: send to the Alerts section webhooks directly, as before.
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
    "agent.metrics_limit_reached": "Metrics limit reached",
    "agent.response_broadcast": "Agent response broadcast",
}


def _alert_message(event_type, details_payload, bridge_name):
    if event_type == "agent.variables_missing":
        variables = details_payload.get("variables")
        names = ", ".join(variables) if isinstance(variables, list | dict) else str(variables or "")
        text = f"Missing variables: {names}" if names else "Required variables were missing"
    elif event_type == "agent.metrics_limit_reached":
        text = f"Metrics limit reached: {details_payload.get('limit_size')}"
    elif event_type == "agent.response_broadcast":
        text = "An agent response was forwarded to webhooks"
    else:
        text = str(details_payload.get("error_message") or details_payload.get("alert") or "")
    text = text[:500]
    return f"{bridge_name}: {text}" if bridge_name else text


async def _emit(event_type, org_id, **kwargs):
    # Lazy import: emit -> queue -> baseQueue imports send_alert lazily too (circular).
    from src.services.notifications.emit import emit_event

    return await emit_event(event_type, org_id, **kwargs)


async def _queue_alert(error_type, org_id, bridge_id, bridge_name, details_payload, payload, user_id, thread_id, service):
    """Queue a customer-facing alert. Returns False when it could not be queued."""
    event_type = LEGACY_ALERT_TYPE_TO_EVENT.get(error_type)
    if not event_type:
        return False
    return await _emit(
        event_type,
        org_id,
        agent_id=bridge_id,
        title=_ALERT_TITLES[event_type],
        message=_alert_message(event_type, details_payload, bridge_name),
        data={
            "thread_id": thread_id,
            "user_id": user_id,
            "service": service,
            # The hub sends this exact body (what sendResponse posted) to the Alerts section
            # webhooks subscribed to legacy_alert_type, and to the GTWY team for error types.
            "legacy_alert_type": error_type,
            "webhook_body": {"error": payload, "success": False, "variables": {}},
        },
        dedupe_key=f"{event_type}:{bridge_id}:{service}",
    )


async def _queue_internal_error(payload, error_location, org_id, bridge_id, bridge_name, error_type, service):
    """Queue an internal error for the GTWY team; the org also sees a short system-error notice.
    Returns False when it could not be queued."""
    team_body = dict(payload)
    if error_location:
        team_body["error_location"] = error_location
    if Config.ENVIRONMENT:
        team_body["ENVIRONMENT"] = Config.ENVIRONMENT
    data = {"team_route": "alerts", "team_body": team_body, "error_type": error_type, "service": service}

    if not org_id:
        return await _emit("ops.internal_error", None, title=str(error_type or "Internal error"), message=str(error_type or "Internal error"), data=data)

    target = f'"{bridge_name}"' if bridge_name else "an agent"
    return await _emit(
        "agent.system_error",
        org_id,
        agent_id=bridge_id,
        title="System error",
        message=f"An internal error interrupted {target}. The GTWY team has been notified.",
        data=data,
        dedupe_key=f"agent.system_error:{bridge_id}:{error_type}",
    )
