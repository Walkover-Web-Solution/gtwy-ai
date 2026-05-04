import asyncio

from globals import logger
from src.configs.constant import redis_keys

from ..cache_service import acquire_lock, delete_in_cache, find_in_cache_with_prefix, make_json_serializable, release_lock
from ..commonServices.baseService.baseService import sendResponse
from ..commonServices.queueService.queueLogService import sub_queue_obj
from ..commonServices.queueService.queueMetricsService import metrics_queue_obj
from src.utils.alert_template import create_response_format
from .ai_middleware_format import process_batch_results
from .batch_script_utils import get_batch_result_handler, is_finalized_batch_item
from .helper import Helper
from globals import *
from .token_calculation import TokenCalculator


async def repeat_function():
    while True:
        await check_batch_status()
        await asyncio.sleep(900)


async def check_batch_status():
    try:
        print("Batch Script running...")
        batch_ids = await find_in_cache_with_prefix("batch_")
        if batch_ids is None:
            return

        for batch_data in batch_ids:
            apikey = batch_data.get('apikey')
            webhook = batch_data.get('webhook')
            batch_id = batch_data.get('id')
            batch_variables = batch_data.get('batch_variables')
            message_id_mapping = batch_data.get('message_id_mapping', {})
            service = batch_data.get('service')
            model = batch_data.get('model')
            org_id = batch_data.get('org_id')
            bridge_id = batch_data.get('bridge_id')
            version_id = batch_data.get('version_id')
            thread_id = batch_data.get('thread_id')
            meta = batch_data.get('meta')

            cache_key = f"{redis_keys['batch_']}{batch_id}"

            # Try to acquire lock for this batch
            lock_acquired = await acquire_lock(batch_id)
            if not lock_acquired:
                logger.info(f"Batch {batch_id} is already being processed by another server, skipping...")
                continue

            try:
                if webhook.get("url") is not None:
                    response_format = create_response_format(webhook.get("url"), webhook.get("headers"))

                try:
                    # Get the appropriate handler for this service
                    batch_result_handler = get_batch_result_handler(service)

                    # Call the service-specific handler
                    results, is_completed = await batch_result_handler(batch_id, apikey)

                    if is_completed:
                        # Batch has reached a terminal state (completed, failed, expired, cancelled)
                        if results:
                            # Process and format the results (could be success or error results)
                            formatted_results = await process_batch_results(
                                results, service, batch_id, batch_variables, message_id_mapping
                            )

                            # Skip webhook for partial/incomplete snapshots; retry on next poll.
                            if not all(is_finalized_batch_item(item) for item in formatted_results):
                                logger.info(
                                    f"Batch {batch_id} has non-finalized items (content null/finish_reason other). "
                                    "Will retry on next poll."
                                )
                                continue

                            # Check if all responses are errors
                            has_success = any(
                                item.get("status_code") is None or item.get("status_code", 200) < 400
                                for item in formatted_results
                            )
                            
                            # Calculate and attach cost to each formatted result's usage before sending webhook
                            for item in formatted_results:
                                item_usage = item.get('usage', {})
                                if item_usage and model:
                                    item_input_tokens = item_usage.get('input_tokens') or 0
                                    item_output_tokens = item_usage.get('output_tokens') or 0
                                    item_cost = 0
                                    if item_input_tokens > 0 or item_output_tokens > 0:
                                        try:
                                            cost_calc = TokenCalculator(service, {})
                                            cost_calc.calculate_usage({'usage': item_usage})
                                            cost_breakdown = cost_calc.calculate_total_cost(model, service)
                                            item_cost = cost_breakdown.get('total_cost', 0) * 0.5
                                        except Exception as cost_err:
                                            logger.error(f"Error calculating cost for batch item: {str(cost_err)}")
                                    item_usage['cost'] = item_cost

                            webhook_response = None
                            webhook_error = None
                            if webhook.get('url') is not None:
                                try:
                                    webhook_response = await sendResponse(response_format, data=formatted_results, success=has_success, meta=meta)
                                    logger.info(f"Batch {batch_id} - webhook sent")
                                except Exception as webhook_err:
                                    webhook_error = str(webhook_err)
                                    logger.error(f"Error sending webhook for batch {batch_id}: {webhook_error}")

                            # Initialize TokenCalculator for batch cost calculation with 50% discount
                            token_calculator = TokenCalculator(service, {})

                            batch_updates = []
                            metrics_data = []

                            for formatted_result in formatted_results:
                                message_id = formatted_result.get('message_id')

                                if not message_id:
                                    continue

                                data = formatted_result.get('data', {})
                                usage = formatted_result.get('usage', {})
                                status_code = formatted_result.get('status_code')
                                error = formatted_result.get('error')

                                is_success = status_code is None or status_code < 400

                                if is_success:
                                    llm_message = data.get('content')
                                    chatbot_message = data.get('content')
                                    error_message = None
                                else:
                                    llm_message = None
                                    chatbot_message = None
                                    if isinstance(error, dict):
                                        error_message = error.get('message', str(error))
                                    elif error:
                                        error_message = str(error)
                                    else:
                                        error_message = "Unknown error occurred"

                                input_tokens = usage.get('input_tokens') or 0 if usage else 0
                                output_tokens = usage.get('output_tokens') or 0 if usage else 0
                                total_tokens = usage.get('total_tokens') or 0 if usage else 0

                                if usage and is_success:
                                    token_calculator.calculate_usage({'usage': usage})

                                update_data = {
                                    'llm_message': llm_message,
                                    'chatbot_message': chatbot_message,
                                    'status': is_success,
                                    'error': error_message,
                                    'finish_reason': data.get('finish_reason'),
                                    'tokens': {
                                        'input_tokens': input_tokens,
                                        'output_tokens': output_tokens,
                                        'total_tokens': total_tokens
                                    } if usage else None,
                                    'batch_data': {
                                        'status': 'completed',
                                        'batch_id': batch_id,
                                        'webhook_response': webhook_response,
                                        'webhook_error': webhook_error,
                                        'webhook_url': webhook.get('url'),
                                        'webhook_headers': Helper.mask_headers(webhook.get('headers'))
                                    }
                                }

                                batch_updates.append({
                                    'batch_id': batch_id,
                                    'message_id': message_id,
                                    'update_data': update_data,
                                })

                                if org_id and model:
                                    individual_cost = 0
                                    if input_tokens > 0 or output_tokens > 0:
                                        try:
                                            temp_calculator = TokenCalculator(service, {})
                                            temp_calculator.calculate_usage({'usage': usage}) if usage else None
                                            cost_breakdown = temp_calculator.calculate_total_cost(model, service)
                                            individual_cost = cost_breakdown.get('total_cost', 0) * 0.5
                                        except Exception as cost_error:
                                            logger.error(f"Error calculating cost for message {message_id}: {str(cost_error)}")
                                            individual_cost = 0

                                    metrics_data.append({
                                        'org_id': org_id,
                                        'bridge_id': bridge_id or '',
                                        'version_id': version_id or '',
                                        'thread_id': thread_id or '',
                                        'model': model,
                                        'input_tokens': float(input_tokens),
                                        'output_tokens': float(output_tokens),
                                        'total_tokens': float(total_tokens),
                                        'apikey_id': '',
                                        'latency': 0,
                                        'success': is_success,
                                        'cost': individual_cost,
                                        'time_zone': 'Asia/Kolkata',
                                        'service': service
                                    })

                            # Publish batch updates to log queue, metrics to metrics queue
                            if batch_updates:
                                try:
                                    await sub_queue_obj.publish_message(make_json_serializable({"update_batch_history": batch_updates}))
                                    logger.info(f"Published {len(batch_updates)} updates for batch {batch_id} to queue")
                                except Exception as queue_error:
                                    logger.error(f"Error publishing batch updates to queue for batch {batch_id}: {str(queue_error)}")

                            if metrics_data:
                                try:
                                    await metrics_queue_obj.publish_message(make_json_serializable({"save_batch_metrics": metrics_data}))
                                    logger.info(f"Published {len(metrics_data)} metrics for batch {batch_id} to metrics queue")
                                except Exception as queue_error:
                                    logger.error(f"Error publishing batch metrics to metrics queue for batch {batch_id}: {str(queue_error)}")
                            
                            await delete_in_cache(cache_key)
                            logger.info(f"Batch {batch_id} completed and removed from cache")
                        else:
                            # No results but marked as completed - send generic error
                            # We cannot update specific logs here as we don't have message_ids
                            # Logs will remain with "under process" status
                            
                            error_response = [{
                                "batch_id": batch_id,
                                "error": {
                                    "message": "Batch completed but no results were returned",
                                    "type": "no_results"
                                },
                                "status_code": 500
                            }]
                            
                            if webhook.get('url') is not None:
                                try:
                                    await sendResponse(response_format, data=error_response, success=False, meta=meta)
                                    logger.info(f"Batch {batch_id} no-results webhook sent")
                                except Exception as webhook_err:
                                    logger.error(f"Error sending webhook for batch {batch_id} (no results case): {str(webhook_err)}")

                            # Delete the key
                            await delete_in_cache(cache_key)
                            logger.info(f"Batch {batch_id} completed and removed from cache")
                        
                    else:
                        # Batch still in progress, will check again on next poll
                        logger.info(f"Batch {batch_id} still in progress")

                except Exception as error:
                    logger.error(f"Error processing batch {batch_id}: {str(error)}")
            finally:
                # Always release the lock, even if an error occurred
                await release_lock(batch_id)

    except Exception as error:
        logger.error(f"An error occurred while checking the batch status: {str(error)}")
