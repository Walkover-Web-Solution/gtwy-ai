import asyncio
import copy
import traceback
from ..utils.ai_middleware_format import send_alert
from src.configs.constant import service_name

async def execute_api_call(
    configuration,
    api_call,
    execution_time_logs,
    timer,
    bridge_id=None,
    message_id=None,
    org_id=None,
    alert_on_retry=False,
    name = "",
    org_name= "",
    service = "",
    count = 0,
    token_calculator = None
):
    try:
        # Start timer
        timer.start()

        # Execute the API call (no retry/fallback)
        config = copy.deepcopy(configuration)
        result = await api_call(config)

        # Log execution time
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})

        if result['success']:
            result['response'] = await check_space_issue(result['response'], service)
            token_calculator.calculate_usage(result['response'])
            return result
        else:
            print("API call failed with error:", result['error'])
            traceback.print_exc()
            
            # Send alert if required (even on failure)
            if alert_on_retry:
                await send_alert(data={
                    "org_name" : org_name,
                    "bridge_name" : name,
                    "configuration": configuration,
                    "message_id": message_id,
                    "bridge_id": bridge_id,
                    "org_id": org_id,
                    "message": "API call failed - no retry attempted",
                    "error" : result.get('error')
                })
            
            return result

    except Exception as e:
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})
        print("execute_api_call error=>", e)
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }

async def check_space_issue(response, service=None):
    
    
    content = None
    if service == service_name['openai_completion'] or service == service_name['groq'] or service == service_name['grok'] or service == service_name['open_router'] or service == service_name['mistral'] or service == service_name['gemini'] or service == service_name['ai_ml']:
        content = response.get("choices", [{}])[0].get("message", {}).get("content", None)
    elif service == service_name['anthropic']:
        content = response.get("content", [{}])
        if content:
            content = content[0].get("text", None)
        else:
            content = None
    elif service == service_name['openai']:
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
    
    if parsed_data == '' and content:
        response['alert_flag'] = True
        text = 'AI is Hallucinating and sending \'\n\' please check your prompt and configurations once'
        if service == service_name['openai_completion'] or service == service_name['groq'] or service == service_name['grok'] or service == service_name['open_router'] or service == service_name['mistral'] or service == service_name['gemini'] or service == service_name['ai_ml']:
            response["choices"][0]["message"]["content"] = text
        elif service == service_name['anthropic']:
            response["content"][0]["text"] = text
        elif service == service_name['openai']:
            if response.get("output", [{}])[0].get("type") == "function_call":
                response["output"][0]["content"][0]["text"] = text
            else:
                for i, item in enumerate(response.get("output", [])):
                    if item.get("type") == "message":
                        response["output"][i]["content"][0]["text"] = text
                        break
    return response
