from groq import AsyncGroq
import traceback
from ..api_executor import execute_api_call
from globals import *

async def groq_runmodel(configuration, apiKey, execution_time_logs, bridge_id, timer, message_id, org_id, name = "", org_name = "", service = "", count = 0, token_calculator=None):
    try:
        # Initialize async client
        groq_client = AsyncGroq(api_key=apiKey)

        # Define the API call function
        async def api_call(config):
            try:
                response = await groq_client.chat.completions.create(**config)
                return {'success': True, 'response': response.to_dict()}
            except Exception as error:
                return {
                    'success': False,
                    'error': str(error),
                    'status_code': getattr(error, 'status_code', None)
                }

        # Execute API call with monitoring
        return await execute_api_call(
            configuration = configuration,
            api_call= api_call,
            execution_time_logs=execution_time_logs,
            timer = timer,
            bridge_id = bridge_id,
            message_id= message_id,
            org_id = org_id,
            alert_on_retry= False,
            name = name,
            org_name = org_name ,
            service = service,
            count = count,
            token_calculator = token_calculator
        )

    except Exception as e:
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})
        logger.error("Groq runmodel error=>", e)
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }

async def groq_test_model(configuration, api_key): 
    groq_client = AsyncGroq(api_key = api_key)
    try:
        response = await groq_client.chat.completions.create(**configuration)
        return {'success': True, 'response': response.to_dict()}
    except Exception as error:
        return {
            'success': False,
            'error': str(error),
            'status_code': getattr(error, 'status_code', None)
        }