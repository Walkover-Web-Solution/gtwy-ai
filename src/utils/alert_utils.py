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


def build_base_payload(bridge_id, org_id, bridge_name, org_name, error_type, api_name, is_playground, error_log, service):
    payload = {
        **error_log,
        "agent_id": bridge_id,
        "org_id": org_id,
        "bridge_name": bridge_name,
        "org_name": org_name,
        "alert_type": error_type,
        "api_name": api_name,
        "service": service,
        "is_playground": is_playground if is_playground is not None else False,
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


def build_webhook_payload(details_payload, error_type, bridge_id, org_id, org_name, user_id, thread_id, service, is_playground, api_name, bridge_name, is_embed):
    payload = {
        "details": details_payload,
        "alert_type": error_type,
        "agent_id": bridge_id,
        "org_id": org_id,
        "org_name": org_name,
        "user_id": user_id,
        "thread_id": thread_id,
        "service": service,
        "source": "playground" if is_playground else "api",
    }
    
    if api_name is not None:
        payload["api_name"] = api_name
    if bridge_name is not None:
        payload["bridge_name"] = bridge_name
    if is_embed is not None:
        payload["is_embed"] = is_embed
    if is_playground is not None:
        payload["is_playground"] = is_playground
    
    return payload

async def send_internal_alert(payload, error_location):
    if error_location:
        payload["error_location"] = error_location
    if Config.ENVIROMENT:
        payload["ENVIROMENT"] = Config.ENVIROMENT
    
    await fetch(DEFAULT_WEBHOOK_URL, method="POST", json_body=payload)


async def send_external_alert(webhook_url, headers, error_type, payload, response, user_question, variables):
    response_format = create_response_format(webhook_url, headers)
    await sendResponse(response_format, data=payload)
