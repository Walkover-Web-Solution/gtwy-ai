from globals import logger
from config import Config
from src.services.proxy.Proxyservice import get_user_org_mapping
from ...db_services.webhook_alert_Dbservice import get_webhook_data
from .apiservice import fetch

_DEFAULT_WEBHOOKS = [
    {
        "name": "Default alert",
        "webhookConfiguration": {"url": "https://flow.sokt.io/func/scriYP8m551q", "headers": {}},
        "alertType": ["Error", "Variable", "retry_mechanism", "code_error", "llm_error", "llm_hallucination"],
        "bridges": ["all"],
        "is_internal": True,
    }
]

async def send_alert_notification(
    alert_type,
    bridge_id=None,
    org_id=None,
    org_name=None,
    bridge_name=None,
    configuration=None,
    message_id=None,
    error_log=None,
    error_message=None,
    message=None,
    is_embed=None,
    user_id=None,
    thread_id=None,
    service=None,
    response=None,
    user_question=None,
    variables=None,
    apikey_name=None,
    apikey_object_id=None,
):
    try:
        error_data = error_log or error_message
        
        if alert_type=="code_error":
            webhook_data = [{**entry, "org_id": org_id} for entry in _DEFAULT_WEBHOOKS]
        else:
            result = await get_webhook_data(org_id)
            webhook_data = result.get("webhook_data", []) + [
                {**entry, "org_id": org_id} for entry in _DEFAULT_WEBHOOKS
            ]

        payload = _build_alert_payload(
            alert_type=alert_type,
            org_name=org_name,
            bridge_name=bridge_name,
            configuration=configuration,
            message_id=message_id,
            bridge_id=bridge_id,
            org_id=org_id,
            message=message,
            error_data=error_data,
            user_id=user_id,
            thread_id=thread_id,
            service=service,
            is_embed=is_embed,
            response=response,
            user_question=user_question,
            variables=variables,
            apikey_name=apikey_name,
            apikey_object_id=apikey_object_id,
        )

        if user_id and is_embed:
            from .helper import Helper

            userinfo = await get_user_org_mapping(user_id, org_id)
            embed_user_id = Helper.extract_embed_user_id(userinfo, org_id)
            if embed_user_id:
                payload["embeduserId"] = embed_user_id

        for entry in webhook_data:
            webhook_config = entry.get("webhookConfiguration", {})
            bridges = entry.get("bridges", [])
            alert_types = entry.get("alertType", [])
            if alert_type not in alert_types:
                continue
            if bridge_id not in bridges and "all" not in bridges:
                continue
            if alert_type == "metrix_limit_reached" and entry.get("limit", 500) == (
                error_data or 0
            ):
                continue

            webhook_url = entry.get("user_url") or webhook_config.get("url")
            if not webhook_url:
                continue
            headers = webhook_config.get("headers", {})

            await fetch(webhook_url, method="POST", headers=headers, json_body=payload)

    except Exception as error:
        logger.error(f"Error in send_alert_notification: {str(error)}")


def _build_alert_payload(
    alert_type,
    org_name,
    bridge_name,
    configuration,
    message_id,
    bridge_id,
    org_id,
    message,
    error_data,
    user_id,
    thread_id,
    service,
    is_embed,
    response,
    user_question,
    variables,
    apikey_name=None,
    apikey_object_id=None,
):
    payload = {
        "alert_type": alert_type,
        "org_name": org_name,
        "bridge_name": bridge_name,
        "bridge_id": bridge_id,
        "org_id": org_id,
    }

    optional_fields = {
        "message_id": message_id,
        "message": message,
        "configuration": configuration,
        "error": error_data,
        "response": response,
        "user_question": user_question,
        "variables": variables,
        "user_id": user_id,
        "thread_id": thread_id,
        "service": service,
        "apikey_name": apikey_name,
        "apikey_object_id": apikey_object_id.get(service) if isinstance(apikey_object_id, dict) and service else apikey_object_id,
    }
    payload.update({k: v for k, v in optional_fields.items() if v})

    payload["ENVIROMENT"] = Config.ENVIROMENT
    if is_embed is not None:
        payload["is_embed"] = is_embed
    return payload
