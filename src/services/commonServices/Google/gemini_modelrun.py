from openai import AsyncOpenAI
import traceback
from ..api_executor import execute_api_call
# from src.services.utils.unified_token_validator import validate_gemini_token_limit
from globals import *


async def gemini_modelrun(configuration, apiKey, execution_time_logs, bridge_id, timer, message_id=None, org_id=None, name = "", org_name= "", service = "", count=0, token_calculator=None):
    try:
        # Validate token count before making API call
        # model_name = configuration.get('model')
        # validate_gemini_token_limit(configuration, model_name, service, apiKey)
        
        gemini = AsyncOpenAI(api_key=apiKey, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")

        # Define the API call function
        async def api_call(config):
            try:
                chat_completion = await gemini.chat.completions.create(**config)
                return {'success': True, 'response': chat_completion.to_dict()}
            except Exception as error:
                return {
                    'success': False,
                    'error': str(error),
                    'status_code': getattr(error, 'status_code', None)
                }

        # Execute API call with monitoring
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=True,
            name = name,
            org_name = org_name,
            service = service,
            count = count,
            token_calculator = token_calculator
        )

    except Exception as error:
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})
        logger.error("runModel error=>", error)
        traceback.print_exc()
        return {
            'success': False,
            'error': str(error)
        }