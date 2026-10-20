import asyncio
import copy
import random
import traceback
from src.configs.service_registry import has_anthropic_shape, has_gemini_shape, has_openai_choices_shape, has_openai_responses_shape
from src.services.commonServices.baseService.utils import serialize_config
from src.exceptions import ApiCallError

RATE_LIMIT_STATUS_CODE = 429


def is_rate_limited(result):
    """True when a call result is a provider rate-limit (429) failure."""
    return isinstance(result, dict) and not result.get("success") and result.get("status_code") == RATE_LIMIT_STATUS_CODE


def _backoff_delay(attempt, base_delay, max_delay, retry_after=None):
    """Full-jitter exponential backoff; a provider supplied retry delay wins but is still capped."""
    if retry_after is not None:
        return min(float(retry_after), max_delay)
    return random.uniform(0, min(base_delay * (2 ** attempt), max_delay))


async def retry_on_rate_limit(call, *, max_attempts=3, base_delay=0.5, max_delay=8.0):
    """Run `call()` and retry it while it reports a 429, sleeping with exponential backoff.

    `call` is an argument-less coroutine function returning the usual {"success": bool, ...}
    dict. Only a 429 is retried — anything else is returned straight away — and the last
    result is returned once the attempts run out, so the caller keeps deciding what to do
    with it (alert, model fallback, ...).
    """
    result = None
    for attempt in range(max(1, max_attempts)):
        result = await call()
        if not is_rate_limited(result) or attempt == max_attempts - 1:
            return result
        await asyncio.sleep(_backoff_delay(attempt, base_delay, max_delay, result.get("retry_after")))
    return result


async def execute_api_call(
    configuration,
    api_call,
    execution_time_logs,
    timer,
    bridge_id=None,
    message_id=None,
    org_id=None,
    alert_on_retry=False,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
    is_embed=None,
    user_id=None,
    thread_id=None,
    api_collection=None,
    is_playground=False,
    retry_policy=None,
):
    try:
        attempt = 0

        async def run_once():
            # Each attempt is timed and logged on its own; only retried attempts carry an index
            # so the log line of a one-shot call stays exactly as it was.
            nonlocal attempt
            timer.start()
            step = f"{service} Processing time for call :- {count + 1}"
            if attempt:
                step = f"{step} (attempt {attempt + 1})"
            attempt += 1
            call_result = await api_call(copy.deepcopy(configuration))
            execution_time_logs.append({"step": step, "time_taken": timer.stop("API chat completion")})
            return call_result

        # Only providers that opt in get retries; everyone else keeps the single-shot behaviour.
        if retry_policy:
            result = await retry_on_rate_limit(run_once, **retry_policy)
        else:
            result = await run_once()

        if result["success"]:
            result["response"] = await check_space_issue(result["response"], service)
            token_calculator.calculate_usage(result["response"])
            return result
        else:
            print("API call failed with error:", result["error"])
            traceback.print_exc()

            # Send alert if required (even on failure)
            if alert_on_retry and not is_playground:
                from src.send_alert import send_alert
                from src.configs.constant import alert_types
                asyncio.create_task(send_alert(
                    bridge_id=bridge_id,
                    org_id=org_id,
                    error_log={"error": result.get("error"), "message": "Exception for the code", "message_id": message_id},
                    error_type=alert_types["retry_mechanism"],
                    bridge_name=name,
                    org_name=org_name,
                    is_embed=is_embed,
                    user_id=user_id,
                    thread_id=thread_id,
                    service=service,
                    api_collection=api_collection,
                    is_external_error=False,
                ))

            return result

    except Exception as e:
        execution_time_logs.append(
            {
                "step": f"{service} Processing time for call :- {count + 1}",
                "time_taken": timer.stop("API chat completion"),
            }
        )
        raise ApiCallError(str(e), status_code=getattr(e, "status_code", None), service=service) from e


async def check_space_issue(response, service=None):
    content = None
    if has_openai_choices_shape(service):
        content = response.get("choices", [{}])[0].get("message", {}).get("content", None)

    elif has_gemini_shape(service):
        content = response["candidates"][0]["content"]["parts"][0]["text"]

    elif has_anthropic_shape(service):
        content = response.get("content", [{}])
        if content:
            content = content[0].get("text", None)
        else:
            content = None
    elif has_openai_responses_shape(service):
        output_list = response.get("output", [])
        if output_list:
            first_output = output_list[0]
            if first_output.get("type") == "function_call":
                content_list = first_output.get("content", [])
                content = content_list[0].get("text", None) if content_list else None
            else:
                # Find first message type item
                for item in output_list:
                    if item.get("type") == "message":
                        content_list = item.get("content", [])
                        content = content_list[0].get("text", None) if content_list else None
                        break
        else:
            content = None

    if content is None:
        return response

    parsed_data = content.replace(" ", "").replace("\n", "")

    if parsed_data == "" and content:
        response["alert_flag"] = True
        text = "AI is Hallucinating and sending '\n' please check your prompt and configurations once"
        if has_openai_choices_shape(service):
            response["choices"][0]["message"]["content"] = text
        elif has_gemini_shape(service):
            response["candidates"][0]["content"]["parts"][0]["text"] = text
        elif has_anthropic_shape(service):
            response["content"][0]["text"] = text
        elif has_openai_responses_shape(service):
            if response.get("output", [{}])[0].get("type") == "function_call":
                response["output"][0]["content"][0]["text"] = text
            else:
                for i, item in enumerate(response.get("output", [])):
                    if item.get("type") == "message":
                        response["output"][i]["content"][0]["text"] = text
                        break
    return response
