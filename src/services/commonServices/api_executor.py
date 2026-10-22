import asyncio
import copy
import traceback
from src.configs.service_registry import has_anthropic_shape, has_gemini_shape, has_openai_choices_shape, has_openai_responses_shape
from src.services.commonServices.baseService.utils import serialize_config
from src.exceptions import ApiCallError


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
):
    try:
        # Time only the API call and stop exactly once: a second stop would pop the
        # request's own start entry and then raise "Timer was not started".
        timer.start()
        try:
            # Execute the API call (no retry/fallback)
            config = copy.deepcopy(configuration)
            result = await api_call(config)
        finally:
            execution_time_logs.append(
                {
                    "step": f"{service} Processing time for call :- {count + 1}",
                    "time_taken": timer.stop("API chat completion"),
                }
            )

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
        raise ApiCallError(str(e), status_code=getattr(e, "status_code", None), service=service) from e


def _enum_value(value):
    """google-genai dumps finish/block reasons as enums; return the plain string."""
    return getattr(value, "value", value)


async def check_space_issue(response, service=None):
    content = None
    if has_openai_choices_shape(service):
        content = response.get("choices", [{}])[0].get("message", {}).get("content", None)

    elif has_gemini_shape(service):
        candidates = response.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        if not parts:
            # Gemini answers 200 with no parts when thinking uses up the output token
            # limit, on safety/recitation blocks, or when the prompt itself is blocked.
            block_reason = _enum_value((response.get("prompt_feedback") or {}).get("block_reason"))
            finish_reason = _enum_value(candidate.get("finish_reason"))
            reason = f"prompt blocked: {block_reason}" if block_reason else f"finish_reason: {finish_reason}"
            raise ApiCallError(f"Gemini returned no content ({reason})", service=service)
        content = parts[0].get("text")

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
