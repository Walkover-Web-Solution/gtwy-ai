from config import Config
from src.configs.constant import api_key_status, service_name
from src.db_services.api_key_status_service import update_apikey_status
from src.services.commonServices.baseService.utils import send_message


def _make_mapper(
    invalid: tuple = (),
    unauthorized: tuple = (),
    limited: tuple = (),
):
    """Generate a mapper function from sets of HTTP codes per status."""
    def mapper(code: int) -> str:
        if code in invalid:      return api_key_status["invalid"]
        if code in unauthorized: return api_key_status["unauthorized"]
        if code in limited:      return api_key_status["limited"]
        if 500 <= code < 600:    return api_key_status["service_down"]
        return api_key_status["working"]
    return mapper


SERVICE_MAPPERS = {
    service_name["openai"]:            _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
    service_name["openai_completion"]: _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
    service_name["gemini"]:            _make_mapper(invalid=(400,),          unauthorized=(403,), limited=(429,)),
    service_name["anthropic"]:         _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
    service_name["groq"]:              _make_mapper(invalid=(400, 401),      unauthorized=(403,), limited=(422, 429, 498)),
    service_name["grok"]:              _make_mapper(invalid=(400, 401),      unauthorized=(403,), limited=(429,)),
    service_name["mistral"]:           _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
    service_name["open_router"]:       _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(402, 429)),
    service_name["deepgram"]:          _make_mapper(invalid=(400, 401, 404), unauthorized=(403,), limited=(402, 413, 422, 429)),
    service_name["neev_cloud"]:        _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
    service_name["moonshot"]:         _make_mapper(invalid=(401,),          unauthorized=(403,), limited=(429,)),
}


def get_api_key_status(service: str, code: int) -> str:
    return SERVICE_MAPPERS[service](code)


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
