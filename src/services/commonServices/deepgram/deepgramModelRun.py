from functools import lru_cache

from src.exceptions import ApiCallError

from ..api_executor import execute_api_call
from deepgram import AsyncDeepgramClient


@lru_cache(maxsize=32)
def _get_deepgram_client(api_key: str) -> AsyncDeepgramClient:
    return AsyncDeepgramClient(api_key=api_key)


async def deepgram_runmodel(
    configuration,
    api_key,
    execution_time_logs,
    bridge_id,
    timer,
    message_id,
    org_id,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
):
    async def api_call(config):
        try:
            deepgram_client = _get_deepgram_client(api_key)
            audio_url = config.pop("audio_url", None)
            sdk_timer_started = False
            try:
                timer.start()
                sdk_timer_started = True

                response = await deepgram_client.listen.v1.media.transcribe_url(url=audio_url, **config)
            finally:
                if sdk_timer_started:
                    execution_time_logs.append(
                        {
                            "step": "deepgram SDK transcribe_url",
                            "time_taken": timer.stop("deepgram SDK transcribe_url"),
                        }
                    )

            response_dict = response.model_dump()
            results = response_dict.get("results", {})
            channels = results.get("channels", [])
            alternatives = channels[0].get("alternatives", []) if channels else []
            transcript = alternatives[0].get("transcript", "") if alternatives else ""
            if not (transcript or "").strip():
                return {
                    "success": False,
                    "error": "No transcript returned by Deepgram for the provided audio URL",
                    "status_code": 400,
                }

            return {"success": True, "response": response_dict}
        except Exception as error:
            # Deepgram SDK may use either status_code or status on error objects
            status = getattr(error, "status_code", None) or getattr(error, "status", 400)
            return {"success": False, "error": str(error), "status_code": status}

    try:
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=False,
            name=name,
            org_name=org_name,
            service=service,
            count=count,
            token_calculator=token_calculator,
        )
    except Exception as error:
        raise ApiCallError(str(error), status_code=getattr(error, "status_code", None), service=service) from error
