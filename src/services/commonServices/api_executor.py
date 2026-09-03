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
        # Start timer
        timer.start()

        # Execute the API call (no retry/fallback)
        config = copy.deepcopy(configuration)
        result = await api_call(config)

        # Log execution time
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
