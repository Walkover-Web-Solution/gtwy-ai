from config import Config
from src.configs.constant import api_key_status
from src.configs.service_registry import apikey_status_codes
from src.db_services.api_key_status_service import update_apikey_status
from src.services.commonServices.baseService.utils import send_message


def get_api_key_status(service: str, code: int) -> str:
    """Map an HTTP status code to an api-key status using the service's
    DB-configured ``apikey_status_codes`` (invalid/unauthorized/limited code lists).

    Previously a hardcoded per-service mapper table, which raised KeyError for any
    service not listed in it — the one thing that made registering a service in the
    DB insufficient on its own.
    """
    codes = apikey_status_codes(service)
    if code in codes.get("invalid", ()):
        return api_key_status["invalid"]
    if code in codes.get("unauthorized", ()):
        return api_key_status["unauthorized"]
    if code in codes.get("limited", ()):
        return api_key_status["limited"]
    if 500 <= code < 600:
        return api_key_status["service_down"]
    return api_key_status["working"]


async def mark_apikey_status_from_response(service, parsed_data, code=None):
    apikey_map = parsed_data.get("apikey_object_id") or {}
    status_map = parsed_data.get("apikey_status") or {}
    apikey_id  = apikey_map.get(service)

    if not apikey_id:
        return

    if code is None:
        new_status = api_key_status["working"]
    else:
        new_status = get_api_key_status(service, int(code))

    if status_map.get(service) == new_status:
        return  # already up to date; skip DB write

    updated = await update_apikey_status(apikey_id, new_status)

    if updated:
        await _notify_apikey_status_change(parsed_data, apikey_id, new_status, service)


async def _notify_apikey_status_change(parsed_data, apikey_id, new_status, service):
    org_id = parsed_data.get("org_id")
    if not org_id:
        return
    await send_message(
        cred={"channel": f"org_{org_id}", "apikey": Config.RTLAYER_AUTH},
        data={
            "type":      "apikey_status_update",
            "apikey_id": apikey_id,
            "status":    new_status,
            "service":   service,
        },
    )
