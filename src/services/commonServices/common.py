from config import Config
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import traceback
import pydash as _
import uuid
import asyncio
from ..utils.helper import Helper
from .baseService.utils import sendResponse
from ..utils.ai_middleware_format import Response_formatter
from ..utils.send_error_webhook import send_error_to_webhook
from src.handler.executionHandler import handle_exceptions
from models.mongo_connection import db
import json
from src.services.utils.common_utils import (
    parse_request_body,
    initialize_timer,
    load_model_configuration,
    handle_pre_tools,
    handle_fine_tune_model,
    manage_threads,
    prepare_prompt,
    configure_custom_settings,
    build_service_params,
    build_service_params_for_batch,
    add_default_template,
    filter_missing_vars,
    send_error,
    restructure_json_schema,
    process_background_tasks,
    add_user_in_varaibles,
    process_background_tasks_for_error,
    update_usage_metrics,
    create_latency_object,
    create_history_params,
    add_files_to_parse_data,
    process_background_tasks_for_playground,
    process_variable_state,
    handle_agent_transfer,
    update_cost_and_last_used_in_background,
    setup_agent_pre_tools
)
from src.services.utils.guardrails_validator import guardrails_check
from src.services.utils.rich_text_support import process_chatbot_response
app = FastAPI()
from src.services.utils.helper import Helper
from globals import *
from src.services.cache_service import find_in_cache, store_in_cache
from src.configs.constant import redis_keys

configurationModel = db["configurations"]

@handle_exceptions
async def chat_multiple_agents(request_body):
    try:
        # Extract bridge configurations from the body
        body = request_body.get('body', {})
        bridge_configurations = body.get('bridge_configurations', {})
        
        if not bridge_configurations:
            raise ValueError("No bridge configurations found")

        primary_bridge_id = body.get('bridge_id')
        
        # Check Redis cache for previously used agent with same thread_id and sub_thread_id
        thread_id = body.get('thread_id')
        sub_thread_id = body.get('sub_thread_id') or thread_id
        
        if thread_id and sub_thread_id:
            redis_key = f"{redis_keys['last_transffered_agent_']}{primary_bridge_id}_{thread_id}_{sub_thread_id}"
            cached_agent_id_raw = await find_in_cache(redis_key)
            # Parse JSON string to remove extra quotes added by store_in_cache
            cached_agent_id = None
            if cached_agent_id_raw:
                try:
                    cached_agent_id = json.loads(cached_agent_id_raw)
                except (json.JSONDecodeError, TypeError):
                    # If parsing fails, use the raw value as fallback
                    cached_agent_id = cached_agent_id_raw.strip('"') if isinstance(cached_agent_id_raw, str) else cached_agent_id_raw
            if cached_agent_id and cached_agent_id in bridge_configurations:
                primary_bridge_id = cached_agent_id
                logger.info(f"Using cached agent {cached_agent_id} for thread {thread_id}_{sub_thread_id}")
        
        if not primary_bridge_id or primary_bridge_id not in bridge_configurations:
            # Use the first agent as primary
            primary_bridge_id = next(iter(bridge_configurations.keys()))
        
        primary_config = bridge_configurations[primary_bridge_id]
        
        # Create a new body for the primary agent
        primary_body = body.copy()
        primary_body.update(primary_config)
        primary_body['bridge_id'] = primary_bridge_id
        # Store the original primary_bridge_id for Redis key consistency
        primary_body['primary_bridge_id'] = primary_bridge_id
        
        # Create a complete request_body structure for the primary agent
        primary_request_body = {
            'body': primary_body,
            'state': request_body.get('state', {}).copy(),
            'path_params': request_body.get('path_params', {})
        }
        
        # Call the chat function for the primary agent only
        result = await chat(primary_request_body)
        
        # Return the result directly
        return result
        
    except Exception as error:
        logger.error(f'Error in chat_multiple_agents: {str(error)}, {traceback.format_exc()}')
        error_object = {
            "success": False,
            "error": f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai"
        }
        return JSONResponse(status_code=500, content=error_object)


@handle_exceptions
async def chat(request_body): 
    result ={}
    class_obj= {}
    try:
        # Store bridge_configurations for potential transfer logic
        bridge_configurations = request_body.get('body', {}).get('bridge_configurations', {})
        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)
        
        # Setup pre_tools for the current agent with its own variables
        setup_agent_pre_tools(parsed_data, bridge_configurations)
        
        # Initialize or retrieve transfer_request_id for tracking transfers
        transfer_request_id = parsed_data.get('transfer_request_id') or str(uuid.uuid1())
        parsed_data['transfer_request_id'] = transfer_request_id
        
        # Initialize transfer history for this request if not exists
        if transfer_request_id not in TRANSFER_HISTORY:
            TRANSFER_HISTORY[transfer_request_id] = []
        if parsed_data.get('guardrails',{}).get('is_enabled', False):
            guardrails_result = await guardrails_check(parsed_data)
            if guardrails_result is not None:
                # Content was blocked by guardrails, return the blocked response
                return JSONResponse(status_code=200, content=guardrails_result)

        parsed_data['configuration']['prompt'] = add_default_template(parsed_data.get('configuration', {}).get('prompt', ''))
        parsed_data['variables'] = add_user_in_varaibles(parsed_data['variables'], parsed_data['user'])
        # Step 2: Initialize Timer
        timer = initialize_timer(parsed_data['state'])
        
        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data['model'], parsed_data['configuration'], parsed_data['service'],
        )
        # Step 3: Load Model Configuration
        await handle_fine_tune_model(parsed_data, custom_config)

        # Step 4: Handle Pre-Tools Execution
        await handle_pre_tools(parsed_data)

        # Step 5: Manage Threads
        thread_info = await manage_threads(parsed_data)
        # add Files from cache is Present
        if len(parsed_data['files']) == 0:
            parsed_data['files'] = await add_files_to_parse_data(parsed_data['thread_id'], parsed_data['sub_thread_id'], parsed_data['bridge_id'])

        # Step 6: Check and add default values for variables based on variable_state
        process_variable_state(parsed_data)
        
        # Step 7: Prepare Prompt, Variables and Memory
        memory, missing_vars = await prepare_prompt(parsed_data, thread_info, model_config, custom_config)
        

        missing_vars = filter_missing_vars(missing_vars, parsed_data['variables_state'])

        # Handle missing variables
        if missing_vars:
            send_error(parsed_data['bridge_id'], parsed_data['org_id'], missing_vars, error_type='Variable', bridge_name=parsed_data.get('name'), is_embed=parsed_data.get('is_embed'), user_id=parsed_data.get('user_id'),thread_id=parsed_data.get('thread_id'),     
        service=parsed_data.get('service') )
        
        # Step 8: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config['configuration'], custom_config, parsed_data['service']
        )
        # Step 9: Execute Service Handler
        params = build_service_params(
            parsed_data, custom_config, model_output_config, thread_info, timer, memory, send_error_to_webhook, bridge_configurations
        )
        # Step 10: json_schema service conversion
        if 'response_type' in custom_config and custom_config['response_type'].get('type') == 'json_schema':
            custom_config['response_type'] = restructure_json_schema(custom_config['response_type'], parsed_data['service'])
        
        
        # Execute with retry mechanism
        class_obj = await Helper.create_service_handler(params, parsed_data['service'])
        
        original_exception = None
        try:
            result = await class_obj.execute()
            
            # Check if agent transfer is needed
            transfer_agent_config = result.get('transfer_agent_config')
            if transfer_agent_config and transfer_agent_config.get('action_type') == 'transfer':
                # Get the correct version_id from bridge_configurations for this agent
                current_version_id = bridge_configurations.get(parsed_data['bridge_id'], {}).get('version_id', parsed_data['version_id'])
                
                # Calculate tokens and create latency BEFORE storing history for transfer
                if parsed_data.get('type') != 'image':
                    parsed_data['tokens'] = params['token_calculator'].calculate_total_cost(parsed_data['model'], parsed_data['service'])
                    result['response']['usage']['cost'] = parsed_data['tokens'].get('total_cost') or 0
                
                # Create latency and update usage metrics BEFORE storing history for transfer
                latency = create_latency_object(timer, params)
                update_usage_metrics(parsed_data, params, latency, result=result, success=True)
                
                # Store current agent's history data before transferring
                current_history_data = {
                    'bridge_id': parsed_data['bridge_id'],
                    'history_params': result.get('historyParams'),
                    'dataset': [parsed_data['usage']],
                    'version_id': current_version_id,
                    'thread_info': thread_info,
                    'parent_id': parsed_data.get('parent_bridge_id', '')
                }
                TRANSFER_HISTORY[transfer_request_id].append(current_history_data)
                
                # Handle agent transfer
                transfer_result = await handle_agent_transfer(
                    result, 
                    request_body, 
                    bridge_configurations, 
                    chat, 
                    current_bridge_id=parsed_data['bridge_id'],
                    transfer_request_id=transfer_request_id
                )
                if transfer_result is not None:
                    return transfer_result
            
            result['response']['usage'] = params['token_calculator'].get_total_usage()
            execution_failed = not result["success"]
            original_error = result.get('error', 'Unknown error') if execution_failed else None
        except Exception as execution_exception:
            # Handle exceptions during execution
            execution_failed = True
            original_error = str(execution_exception)
            original_exception = execution_exception
            logger.error(f"Initial execution failed with {parsed_data['service']}/{parsed_data['model']}: {original_error}")
            result = {
                "success": False,
                "error": original_error,
                "response": {"usage": {}},
                "modelResponse": {}
            }
        
        # Retry mechanism with fallback configuration
        if execution_failed and parsed_data.get('fall_back') and parsed_data['fall_back'].get('is_enable', False):
            try:
                # Store original configuration
                fallback_config = parsed_data['fall_back']
                original_model = parsed_data['model']
                original_service = parsed_data['service']
                
                # Update parsed_data with fallback configuration
                parsed_data['model'] = fallback_config.get('model', parsed_data['model'])
                parsed_data['service'] = fallback_config.get('service', parsed_data['service'])
                parsed_data['configuration']['model'] = fallback_config.get('model')
                # Check if service has changed - if so, create new service handler
                if parsed_data['service'] != original_service:
                    parsed_data['apikey'] = fallback_config.get('apikey')
                    if parsed_data['apikey'] is None and fallback_config.get('service') == 'ai_ml':
                        parsed_data['apikey'] = Config.AI_ML_APIKEY
                    
                    # Load fresh model configuration for the fallback service and model
                    fallback_model_config, fallback_custom_config, fallback_model_output_config = await load_model_configuration(
                        parsed_data['model'], parsed_data['configuration'], parsed_data['service']
                    )
            
                    # Configure custom settings specifically for the fallback service
                    fallback_custom_config = await configure_custom_settings(
                        fallback_model_config['configuration'], fallback_custom_config, parsed_data['service']
                    )
                    params = build_service_params(
                        parsed_data, fallback_custom_config, fallback_model_output_config, thread_info, timer, memory, send_error_to_webhook, bridge_configurations
                    )
                    # Step 9 : json_schema service conversion
                    if 'response_type' in fallback_custom_config and fallback_custom_config['response_type'].get('type') == 'json_schema':
                        fallback_custom_config['response_type'] = restructure_json_schema(fallback_custom_config['response_type'], parsed_data['service'])
                    
                    # Create new service handler for the fallback service
                    class_obj = await Helper.create_service_handler(params, parsed_data['service'])
                else:
                    # Same service, just update existing class_obj
                    class_obj.model = parsed_data['model']
                    if fallback_config.get('apikey'):
                        class_obj.apikey = fallback_config['apikey']
                        if class_obj.apikey is None and fallback_config.get('service') == 'ai_ml':
                            class_obj.apikey = Config.AI_ML_APIKEY
                            
                    
                    # Reconfigure custom_config for fallback service
                    class_obj.customConfig = await configure_custom_settings(
                        model_config['configuration'], custom_config, parsed_data['service']
                    )
                
                # Execute with updated configuration
                result = await class_obj.execute()
                result['response']['usage'] = params['token_calculator'].get_total_usage()
                
                # Mark that this was a retry attempt and store original error
                if result["success"]:
                    result['response']['data']['firstAttemptError'] = f"Original attempt failed with {original_service}/{original_model}: {original_error}. Retried with {parsed_data['service']}/{parsed_data['model']}"
                    result['response']['data']['fallback'] = True
                
            except Exception as retry_error:
                # If retry also fails, chain the new exception to the original one
                logger.error(f"Fallback attempt failed with {parsed_data['service']}/{parsed_data['model']}: {retry_error}")
                # Restore original configuration before raising
                parsed_data['model'] = original_model
                parsed_data['service'] = original_service
                raise retry_error from original_exception
        
        if not result["success"]:
            raise ValueError(result)
        # Add message_id to response
        result['response']['data']['message_id'] = parsed_data['message_id']


        if original_error:
            send_error(parsed_data['bridge_id'], parsed_data['org_id'], original_error, error_type='retry_mechanism', bridge_name=parsed_data.get('name'), is_embed=parsed_data.get('is_embed'), user_id=parsed_data.get('user_id'), thread_id=parsed_data.get('thread_id'),      
        service=parsed_data.get('service') )
        
        if parsed_data['configuration']['type'] == 'chat':
            if parsed_data['is_rich_text'] and parsed_data['bridgeType'] and parsed_data['reasoning_model'] == False:
                try:
                    await process_chatbot_response(result, params, parsed_data, model_output_config, timer, params['execution_time_logs'])
                except Exception as e:
                    raise RuntimeError(f"error in chatbot : {e}")
        parsed_data['alert_flag'] = result['modelResponse'].get('alert_flag', False)
        if parsed_data.get('type') != 'image':
            parsed_data['tokens'] = params['token_calculator'].calculate_total_cost(parsed_data['model'], parsed_data['service'])
            result['response']['usage']['cost'] = parsed_data['tokens'].get('total_cost') or 0
        
        # Send data to playground
        if parsed_data.get('is_playground') and parsed_data.get('body', {}).get('bridge_configurations', {}).get('playground_response_format'):
            await sendResponse(parsed_data['body']['bridge_configurations']['playground_response_format'], result["response"], success=True, variables=parsed_data.get('variables',{}))
        
        # Create latency object using utility function
        latency = create_latency_object(timer, params)
        if not parsed_data['is_playground']:
            if result.get('response') and result['response'].get('data'):
                result['response']['data']['message_id'] = parsed_data['message_id']
            await sendResponse(parsed_data['response_format'], result["response"], success=True, variables=parsed_data.get('variables',{}))
            # Update usage metrics for successful API calls
            update_usage_metrics(parsed_data, params, latency, result=result, success=True)
            result['response']['usage']['cost'] = parsed_data['usage'].get('expectedCost', 0)
            
            # Process background tasks (handles both transfer and non-transfer cases)
            await process_background_tasks(parsed_data, result, params, thread_info, transfer_request_id, bridge_configurations)
        else:
            if parsed_data.get('testcase_data',{}).get('run_testcase', False):
                from src.services.commonServices.testcases import process_single_testcase_result
                # Process testcase result and add score to response
                testcase_result = await process_single_testcase_result(
                    parsed_data.get('testcase_data', {}), 
                    result, 
                    parsed_data
                )
                result['response']['testcase_result'] = testcase_result
            else:
                await process_background_tasks_for_playground(result, parsed_data)
        await update_cost_and_last_used_in_background(parsed_data)
        
        # Save agent bridge_id to Redis for 3 days (259200 seconds)
        thread_id = parsed_data.get('thread_id')
        sub_thread_id = parsed_data.get('sub_thread_id')
        bridge_id = parsed_data.get('bridge_id')
        original_primary_bridge_id = request_body.get('body', {}).get('primary_bridge_id')
        
        if thread_id and sub_thread_id and bridge_id:
            # Use original primary bridge_id in key for consistency, but save current bridge_id as value
            redis_key = f"{redis_keys['last_transffered_agent_']}{original_primary_bridge_id}_{thread_id}_{sub_thread_id}"
            # Ensure bridge_id is a clean string without extra quotes
            bridge_id_to_save = str(bridge_id).strip('"\'') if bridge_id else None
            if bridge_id_to_save:
                await store_in_cache(redis_key, bridge_id_to_save, ttl=259200)  # 3 days
            logger.info(f"Cached agent {bridge_id} for thread {thread_id}_{sub_thread_id} with key based on original primary {original_primary_bridge_id}")
        
        return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})
    
    except (Exception, ValueError, BadRequestException) as error:
        if not isinstance(error, BadRequestException):
            logger.error(f'Error in chat service: %s, {str(error)}, {traceback.format_exc()}')
        if not parsed_data['is_playground']:
            # Create latency object and update usage metrics
            latency = create_latency_object(timer, params)
            update_usage_metrics(parsed_data, params, latency, error=error, success=False)
            
            # Create history parameters
            parsed_data['historyParams'] = create_history_params(parsed_data, error, class_obj)
            await sendResponse(parsed_data['response_format'], result.get("error", str(error)), variables=parsed_data['variables']) if parsed_data['response_format']['type'] != 'default' else None
            # Process background tasks for error handling
            await process_background_tasks_for_error(parsed_data, error)
        # Check for a chained exception and create a structured error object
        if error.__cause__:
            # Combine both initial and fallback errors into a single string
            combined_error_string = (
                f"Initial Error: {str(error.__cause__)} (Type: {type(error.__cause__).__name__}). "
                f"Fallback Error: {str(error)} (Type: {type(error).__name__}). "
                f"For more support contact us at support@gtwy.ai"
            )
            error_object = {
                "success": False,
                "error": combined_error_string,
                "message_id": parsed_data.get('message_id')
            }
        else:
            # Single error case
            error_string = f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai"
            error_object = {
                "success": False,
                "error": error_string,
                "message_id": parsed_data.get('message_id')
            }
        if parsed_data['is_playground'] and parsed_data['body']['bridge_configurations'].get('playground_response_format'):
            await sendResponse(parsed_data['body']['bridge_configurations']['playground_response_format'], error_object, success=False, variables=parsed_data.get('variables',{}))
        raise ValueError(error_object)

@handle_exceptions
async def embedding(request_body):
    result = {}
    try:
        body = request_body.get('body')
        configuration = body.get('configuration')
        text = body.get('text')
        model = configuration.get('model')
        service = body.get('service')
        model_config, custom_config, model_output_config = await load_model_configuration(model, configuration, service)
        chatbot = body.get('chatbot')
        if chatbot:
            raise ValueError("Error: Embedding not supported for chatbot")
        params = {
            "model": model,
            "configuration": configuration,
            "model_config": model_config,
            "customConfig": custom_config,
            "model_output_config": model_output_config,
            "text": text,
            "response_format": configuration.get("response_format") or {},
            "service": service,
            "version_id": body.get('version_id'),
            "bridge_id": body.get('bridge_id'),
            "org_id": body.get('org_id'),
            "apikey" : body.get('apikey')
        }

        class_obj = await Helper.embedding_service_handler(params, service)
        result = await class_obj.execute_embedding()

        if not result["success"]:
            raise ValueError(result)
        
        result['modelResponse'] = await Response_formatter(response = result["response"], service = service, type =configuration.get('type'))

        return JSONResponse(status_code=200, content={"success": True, "response": result["modelResponse"]})
    except Exception as error:
        raise ValueError(error)

@handle_exceptions
async def batch(request_body):
    result ={}
    class_obj= {}
    try:
        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)
        if parsed_data['batch_webhook'] is None:
            raise ValueError("webhook is required")
        
        # Validate batch_variables if provided
        batch_variables = parsed_data.get('batch_variables')
        if batch_variables is not None:
            if not isinstance(batch_variables, list):
                raise ValueError("batch_variables must be an array")
            if len(batch_variables) != len(parsed_data['batch']):
                raise ValueError(f"batch_variables array length ({len(batch_variables)}) must match batch array length ({len(parsed_data['batch'])})")
        
        # Step 2: Process prompts with variable replacement for each batch message
        original_prompt = parsed_data['configuration'].get('prompt', '')
        processed_prompts = []
        all_missing_vars = {}
        
        if batch_variables is not None:
            for idx, variables in enumerate(batch_variables):
                # Replace variables in prompt for each message
                # If a variable is not provided, the placeholder remains in the prompt
                processed_prompt, missing_vars = Helper.replace_variables_in_prompt(
                    original_prompt, 
                    variables
                )
                processed_prompts.append(processed_prompt)
                
                # Collect missing variables from all batch items
                if missing_vars:
                    for key, value in missing_vars.items():
                        if key not in all_missing_vars:
                            all_missing_vars[key] = value
        else:
            # No batch_variables provided, use original prompt for all messages
            for _ in parsed_data['batch']:
                processed_prompts.append(original_prompt)
        
        # Send alert if there are any missing variables across all batch items
        if all_missing_vars:
            send_error(
                parsed_data['bridge_id'], 
                parsed_data['org_id'], 
                all_missing_vars, 
                error_type='Variable', 
                bridge_name=parsed_data.get('name'), 
                is_embed=parsed_data.get('is_embed'), 
                user_id=parsed_data.get('user_id')
            )
        
        # Store processed prompts in parsed_data
        parsed_data['processed_prompts'] = processed_prompts
        
        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data['model'], parsed_data['configuration'], parsed_data['service'],
        )

        # Step 4: Handle Pre-Tools Execution
        await handle_pre_tools(parsed_data)
        
        # Step 7: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config['configuration'], custom_config, parsed_data['service']
        )
        if 'tools' in custom_config:
            del custom_config['tools']
        # Step 8: Execute Service Handler
        params = build_service_params_for_batch( parsed_data, custom_config, model_output_config )
        class_obj = await Helper.create_service_handler_for_batch(params, parsed_data['service'])
        result = await class_obj.batch_execute()
            
        if not result["success"]:
            raise ValueError(result)
        
        response_content = {
            "success": True,
            "response": result["message"]
        }
        
        # Include batch_id and messages if available
        if "batch_id" in result:
            response_content["batch_id"] = result["batch_id"]
        if "messages" in result:
            response_content["messages"] = result["messages"]
        
        return JSONResponse(status_code=200, content=response_content)
    except Exception as error:
        traceback.print_exc()
        raise ValueError(error)
    

@handle_exceptions
async def image(request_body):
    result ={}
    class_obj= {}
    try:
        # Store bridge_configurations for potential transfer logic
        bridge_configurations = request_body.get('body', {}).get('bridge_configurations', {})
        
        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)
        
        # Initialize or retrieve transfer_request_id for tracking transfers
        transfer_request_id = parsed_data.get('transfer_request_id') or str(uuid.uuid1())
        parsed_data['transfer_request_id'] = transfer_request_id
        
        # Initialize transfer history for this request if not exists
        if transfer_request_id not in TRANSFER_HISTORY:
            TRANSFER_HISTORY[transfer_request_id] = []

        # Step 2: Initialize Timer
        timer = initialize_timer(parsed_data['state'])
        
        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data['model'], parsed_data['configuration'], parsed_data['service'],
        )
        
        # Step 4: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config['configuration'], custom_config, parsed_data['service']
        )
        # Step 5: Manage Threads
        thread_info = await manage_threads(parsed_data)

        # Step 5: Execute Service Handler
        params = build_service_params(
            parsed_data, custom_config, model_output_config, thread_info, timer, None, send_error_to_webhook, bridge_configurations
        )
        
        
        class_obj = await Helper.create_service_handler(params, parsed_data['service'])
        result = await class_obj.execute()
            
        if not result["success"]:
            raise ValueError(result)

        # Create latency object using utility function
        if result.get('response') and result['response'].get('data'):
            result['response']['data']['id'] = parsed_data['message_id']
        await sendResponse(parsed_data['response_format'], result["response"], success=True, variables=parsed_data.get('variables',{}))
        latency = create_latency_object(timer, params)
        if not parsed_data['is_playground']:
            # Update usage metrics for successful API calls
            update_usage_metrics(parsed_data, params, latency, result=result, success=True)
            # Process background tasks (handles both transfer and non-transfer cases)
            await process_background_tasks(parsed_data, result, params, thread_info, transfer_request_id, bridge_configurations)
        return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})
    
    except (Exception, ValueError, BadRequestException) as error:
        if not isinstance(error, BadRequestException):
            logger.error(f'Error in image service: {str(error)}, {traceback.format_exc()}')
        if not parsed_data['is_playground']:
            # Update parsed_data with thread_info if available and thread_id/sub_thread_id are None
            if 'thread_info' in locals() and thread_info:
                if not parsed_data.get('thread_id') and thread_info.get('thread_id'):
                    parsed_data['thread_id'] = thread_info['thread_id']
                if not parsed_data.get('sub_thread_id') and thread_info.get('sub_thread_id'):
                    parsed_data['sub_thread_id'] = thread_info['sub_thread_id']
            
            # Create latency object and update usage metrics
            latency = create_latency_object(timer, params)
            update_usage_metrics(parsed_data, params, latency, error=error, success=False)
            
            # Create history parameters
            parsed_data['historyParams'] = create_history_params(parsed_data, error, class_obj, thread_info if 'thread_info' in locals() else None)
            await sendResponse(parsed_data['response_format'], result.get("error", str(error)), variables=parsed_data['variables']) if parsed_data['response_format']['type'] != 'default' else None
            # Process background tasks for error handling
            await process_background_tasks_for_error(parsed_data, error)
        # Check for a chained exception and create a structured error object
        if error.__cause__:
            # Combine both initial and fallback errors into a single string
            combined_error_string = (
                f"Initial Error: {str(error.__cause__)} (Type: {type(error.__cause__).__name__}). "
                f"Fallback Error: {str(error)} (Type: {type(error).__name__}). "
                f"For more support contact us at support@gtwy.ai"
            )
            error_object = {
                "success": False,
                "error": combined_error_string,
                "message_id": parsed_data.get('message_id')
            }
        else:
            # Single error case
            error_string = f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai"
            error_object = {
                "success": False,
                "error": error_string,
                "message_id": parsed_data.get('message_id')
            }
        if parsed_data['is_playground'] and parsed_data['body']['bridge_configurations'].get('playground_response_format'):
            await sendResponse(parsed_data['body']['bridge_configurations']['playground_response_format'], error_object, success=False, variables=parsed_data.get('variables',{}))
        raise ValueError(error_object)
