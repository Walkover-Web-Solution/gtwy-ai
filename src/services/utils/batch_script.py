import asyncio

from globals import logger
from src.configs.constant import redis_keys

from ..cache_service import find_in_cache, acquire_lock, delete_in_cache, find_in_cache_with_prefix, make_json_serializable, release_lock
from ..commonServices.baseService.baseService import sendResponse
from ..commonServices.queueService.queueLogService import sub_queue_obj
from ..commonServices.queueService.queueMetricsService import metrics_queue_obj
from src.utils.alert_template import create_response_format
from .ai_middleware_format import process_batch_results
from .batch_script_utils import get_batch_result_handler, is_finalized_batch_item, get_batch_result_data
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

            # ── Step 1: acquire lock ──────────────────────────────────────────
            lock_acquired = await acquire_lock(batch_id)  # TTL now 1800 s
            if not lock_acquired:
                logger.info(f"Batch {batch_id} is already being processed, skipping...")
                continue

            try:
                # ── Step 2: double-checked locking ────────────────────────────
                # Another pod may have completed + deleted the cache key between
                # find_in_cache_with_prefix() and our lock acquisition.
                still_exists = await find_in_cache(cache_key)
                if still_exists is None:
                    logger.info(f"Batch {batch_id} already completed by another pod, skipping...")
                    continue

                if webhook.get("url") is not None:
                    response_format = create_response_format(
                        webhook.get("url"), webhook.get("headers")
                    )

                batch_result_handler = get_batch_result_handler(service)
                results, is_completed = await batch_result_handler(batch_id, apikey)

                if is_completed:
                    if results:
                        # Pre-compute cost per message_id from raw batch results
                        item_costs = {}
                        for raw_item in results:
                            msg_id, result_data, _status_code, has_error = get_batch_result_data(raw_item, service)
                            if msg_id and not has_error and result_data and model:
                                try:
                                    temp_calculator = TokenCalculator(service, {})
                                    temp_calculator.calculate_usage(result_data)
                                    cost_breakdown = temp_calculator.calculate_total_cost(model, service)
                                    item_costs[msg_id] = cost_breakdown.get("total_cost", 0) * 0.5
                                except Exception as cost_error:
                                    logger.error(f"Error calculating batch cost for message {msg_id}: {str(cost_error)}")

                        formatted_results = await process_batch_results(
                            results, service, batch_id, batch_variables, message_id_mapping
                        )

                        if not all(is_finalized_batch_item(item) for item in formatted_results):
                            logger.info(
                                f"Batch {batch_id} has non-finalized items. Will retry on next poll."
                            )
                            continue

                        has_success = any(
                            item.get("status_code") is None or item.get("status_code", 200) < 400
                            for item in formatted_results
                        )

                        # Attach pre-computed cost to each formatted result's usage
                        for item in formatted_results:
                            msg_id = item.get("message_id")
                            item_usage = item.get("usage")
                            if item_usage is not None:
                                item_usage["cost"] = item_costs.get(msg_id, 0)

                        webhook_response = None
                        webhook_error = None

                        # ── Step 3: delete cache BEFORE webhook call ───────────
                        # Deleting here means even if the webhook call is slow,
                        # no other pod can pick this batch up after lock expiry.
                        await delete_in_cache(cache_key)
                        logger.info(f"Batch {batch_id} removed from cache before webhook dispatch")

                        if webhook.get('url') is not None:
                            try:
                                webhook_response = await sendResponse(
                                    response_format, data=formatted_results, success=has_success, meta=meta
                                )
                                logger.info(f"Batch {batch_id} - webhook sent")
                            except Exception as webhook_err:
                                webhook_error = str(webhook_err)
                                logger.error(f"Error sending webhook for batch {batch_id}: {webhook_error}")

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
                            individual_cost = item_costs.get(message_id, 0)

                            update_data = {
                                'llm_message': llm_message,
                                'chatbot_message': chatbot_message,
                                'status': is_success,
                                'error': error_message,
                                'finish_reason': data.get('finish_reason'),
                                'tokens': {
                                    'input_tokens': input_tokens,
                                    'output_tokens': output_tokens,
                                    'total_tokens': total_tokens,
                                    'expected_cost': individual_cost,
                                } if usage else None,
                                'batch_data': {
                                    'status': 'completed',
                                    'batch_id': batch_id,
                                    'webhook_response': webhook_response,
                                    'webhook_error': webhook_error,
                                    'webhook_url': webhook.get('url'),
                                    'webhook_headers': Helper.mask_headers(webhook.get('headers')),
                                }
                            }

                            batch_updates.append({
                                'batch_id': batch_id,
                                'message_id': message_id,
                                'update_data': update_data,
                            })

                            if org_id and model:
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
                                    'service': service,
                                })

                        if batch_updates:
                            try:
                                await sub_queue_obj.publish_message(
                                    make_json_serializable({'update_batch_history': batch_updates})
                                )
                                logger.info(
                                    f"Published {len(batch_updates)} batch history updates for batch {batch_id}"
                                )
                            except Exception as queue_error:
                                logger.error(f"Error publishing batch history for batch {batch_id}: {queue_error}")

                        if metrics_data:
                            try:
                                await metrics_queue_obj.publish_message(
                                    make_json_serializable({'save_metrics': metrics_data})
                                )
                                logger.info(
                                    f"Published {len(metrics_data)} metrics for batch {batch_id}"
                                )
                            except Exception as metrics_error:
                                logger.error(f"Error publishing metrics for batch {batch_id}: {metrics_error}")

                    else:
                        # No results but completed — send generic error webhook
                        error_response = [{
                            "batch_id": batch_id,
                            "error": {"message": "Batch completed but no results were returned", "type": "no_results"},
                            "status_code": 500,
                        }]

                        # ── delete cache before webhook here too ──────────────
                        await delete_in_cache(cache_key)
                        logger.info(f"Batch {batch_id} (no results) removed from cache")

                        if webhook.get('url') is not None:
                            try:
                                await sendResponse(response_format, data=error_response, success=False)
                                logger.info(f"Batch {batch_id} no-results webhook sent")
                            except Exception as webhook_err:
                                logger.error(f"Error sending no-results webhook for batch {batch_id}: {webhook_err}")

                else:
                    logger.info(f"Batch {batch_id} still in progress")

            except Exception as error:
                logger.error(f"Error processing batch {batch_id}: {error}")
            finally:
                # Lock is always released; cache is already deleted above if completed
                await release_lock(batch_id)

    except Exception as error:
        logger.error(f"An error occurred while checking batch status: {error}")
