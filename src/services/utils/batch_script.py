from ..cache_service import find_in_cache_with_prefix, delete_in_cache, acquire_lock, release_lock
from ..utils.send_error_webhook import create_response_format
from ..commonServices.baseService.baseService import sendResponse
import asyncio
from .ai_middleware_format import process_batch_results
from src.configs.constant import redis_keys
from .batch_script_utils import get_batch_result_handler
from globals import *


async def repeat_function():
    while True:
        await check_batch_status()
        await asyncio.sleep(900)


async def check_batch_status():
    try:
        print("Batch Script running...")
        batch_ids = await find_in_cache_with_prefix('batch_')
        if batch_ids is None:
            return
        
        for batch_data in batch_ids:
            apikey = batch_data.get('apikey')
            webhook = batch_data.get('webhook')
            batch_id = batch_data.get('id')
            batch_variables = batch_data.get('batch_variables')
            custom_id_mapping = batch_data.get('custom_id_mapping', {})
            service = batch_data.get('service')
            
            # Try to acquire lock for this batch
            lock_acquired = await acquire_lock(batch_id)
            if not lock_acquired:
                logger.info(f"Batch {batch_id} is already being processed by another server, skipping...")
                continue
            
            try:
                if webhook.get('url') is not None:
                    response_format = create_response_format(webhook.get('url'), webhook.get('headers'))
                
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
                                results, service, batch_id, batch_variables, custom_id_mapping
                            )
                            
                            # Check if all responses are errors
                            has_success = any(
                                item.get("status_code") is None or item.get("status_code", 200) < 400 
                                for item in formatted_results
                            )
                            
                            await sendResponse(response_format, data=formatted_results, success=has_success)
                        else:
                            # No results but marked as completed - send generic error
                            error_response = [{
                                "batch_id": batch_id,
                                "error": {
                                    "message": "Batch completed but no results were returned",
                                    "type": "no_results"
                                },
                                "status_code": 500
                            }]
                            await sendResponse(response_format, data=error_response, success=False)
                        
                        # Delete from cache after sending webhook
                        cache_key = f"{redis_keys['batch_']}{batch_id}"
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
