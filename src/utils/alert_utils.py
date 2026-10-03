import json
from typing import Any

from config import Config
from globals import logger
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

# Request payloads attached to alerts. Slack caps a message at ~40k chars;
# leave room for the rest of the alert.
MAX_PAYLOAD_CHARS = 15000
MAX_STRING_CHARS = 2000
SECRET_KEY_PARTS = ("apikey", "api_key", "authorization", "token", "secret", "password")


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower().replace("-", "_")
    # "max_tokens" / "max_output_tokens" are config, not credentials.
    if lowered.endswith("tokens"):
        return False
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if _is_secret_key(k) else _clean_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    if isinstance(value, str):
        # Inline files (image_url / input_file data URLs) are base64 blobs.
        if value.startswith("data:") and ";base64," in value:
            return f"{value.split(';base64,', 1)[0]};base64,<{len(value)} chars omitted>"
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}…<{len(value) - MAX_STRING_CHARS} chars truncated>"
    return value


def sanitize_payload_for_alert(payload: Any) -> str | None:
    """Return an LLM request payload as a pretty JSON string (redacted, base64-stripped, size-capped), or None."""
    if not payload:
        return None
    try:
        payload_text = json.dumps(_clean_value(payload), indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"sanitize_payload_for_alert: could not serialize request payload: {e}")
        return f"<could not serialize request payload: {e}>"
    if len(payload_text) > MAX_PAYLOAD_CHARS:
        payload_text = f"{payload_text[:MAX_PAYLOAD_CHARS]}\n…<{len(payload_text) - MAX_PAYLOAD_CHARS} chars truncated>"
    return payload_text


def build_base_payload(bridge_id, org_id, bridge_name, org_name, error_type, api_name, error_log, service):
    error_log_dict = error_log if isinstance(error_log, dict) else {"error": error_log}
    payload = {
        **error_log_dict,
        "agent_id": bridge_id,
        "org_id": org_id,
        "bridge_name": bridge_name,
        "org_name": org_name,
        "alert_type": error_type,
        "api_name": api_name,
        "service": service
    }
    return payload


def get_details_payload(error_type, data_source, context):
    payload_map = {
        alert_types["variable"]: lambda: create_missing_vars(data_source or {}, context),
        alert_types["metrix_limit_reached"]: lambda: metrix_limit_reached(data_source or 0, context),
        alert_types["retry_mechanism"]: lambda: create_retry_mechanism_payload(data_source or "", context),
        alert_types["broadcast_response"]: lambda: create_broadcast_response_payload(data_source or {}, context),
    }
    return payload_map.get(error_type, lambda: create_error_payload(data_source or {}, context))()


def build_webhook_payload(details_payload, error_type, bridge_id, org_id, org_name, user_id, thread_id, service, api_name, bridge_name, is_embed):
    payload = {
        "details": details_payload,
        "alert_type": error_type,
        "agent_id": bridge_id,
        "org_id": org_id,
        "org_name": org_name,
        "user_id": user_id,
        "thread_id": thread_id,
        "service": service,
        "source": "api",
    }
    
    if api_name is not None:
        payload["api_name"] = api_name
    if bridge_name is not None:
        payload["bridge_name"] = bridge_name
    if is_embed is not None:
        payload["is_embed"] = is_embed
    
    return payload

async def send_internal_alert(payload, error_location):
    if error_location:
        payload["error_location"] = error_location
    if Config.ENVIRONMENT:
        payload["ENVIRONMENT"] = Config.ENVIRONMENT
    
    await fetch(DEFAULT_WEBHOOK_URL, method="POST", json_body=payload)


async def send_external_alert(webhook_url, headers, error_type, payload, response, user_question, variables):
    response_format = create_response_format(webhook_url, headers)
    await sendResponse(response_format, data=payload)
