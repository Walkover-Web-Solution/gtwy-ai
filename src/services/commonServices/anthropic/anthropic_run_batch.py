import json
import uuid
from anthropic import Anthropic

async def create_batch_requests(batch_requests, apiKey, model):
    """
    Creates and submits a message batch to Anthropic API.
    
    Args:
        batch_requests: List of request dictionaries
        apiKey: Anthropic API key
        model: Model name to use
        
    Returns:
        Message batch object from Anthropic API
    """
    try:
        # Initialize Anthropic client
        client = Anthropic(api_key=apiKey)
        
        # Create message batch
        message_batch = client.messages.batches.create(
            requests=batch_requests
        )
        
        print(f"Created Anthropic batch: {message_batch.id}")
        return message_batch
        
    except Exception as e:
        print("Error in Anthropic create_batch_requests:", repr(e))
        print("Cause:", repr(getattr(e, "__cause__", None)))
        raise

async def retrieve_batch_status(batch_id, apiKey):
    """
    Retrieves the status of an Anthropic message batch.
    
    Args:
        batch_id: Message batch ID
        apiKey: Anthropic API key
        
    Returns:
        Message batch object with current status
    """
    try:
        # Initialize Anthropic client
        client = Anthropic(api_key=apiKey)
        
        # Get batch status
        message_batch = client.messages.batches.retrieve(batch_id)
        print(f"Anthropic batch status: {message_batch.processing_status}")
        return message_batch
        
    except Exception as e:
        print(f"Error in Anthropic retrieve_batch_status: {e}")
        raise

async def retrieve_batch_results(batch_id, apiKey):
    """
    Retrieves the results of a completed Anthropic message batch.
    
    Args:
        batch_id: Message batch ID
        apiKey: Anthropic API key
        
    Returns:
        List of batch results
    """
    try:
        # Initialize Anthropic client
        client = Anthropic(api_key=apiKey)
        
        # Iterate through results
        results = []
        for result in client.messages.batches.results(batch_id):
            results.append(result)
        
        print(f"Retrieved {len(results)} results from Anthropic batch {batch_id}")
        return results
        
    except Exception as e:
        print(f"Error in Anthropic retrieve_batch_results: {e}")
        raise


async def handle_batch_results(batch_id, apikey):
    """
    Handle Anthropic batch processing - retrieve status and process results.
    
    Anthropic batch processing statuses:
    - in_progress: Batch is being processed
    - canceling: Batch is being canceled
    - ended: Batch has finished (may contain succeeded, errored, canceled, or expired results)
    
    Args:
        batch_id: Batch ID
        apikey: Anthropic API key
        
    Returns:
        Tuple of (results, is_completed)
        - For ended batches: (results_list, True) - includes all result types
        - For in_progress/canceling: (None, False) - continues polling
    """
    message_batch = await retrieve_batch_status(batch_id, apikey)
    processing_status = message_batch.processing_status
    
    # In-progress states - continue polling
    if processing_status in ["in_progress", "canceling"]:
        return None, False
    
    # Terminal state - retrieve all results
    if processing_status == "ended":
        # Retrieve batch results (includes succeeded, errored, canceled, expired)
        results = await retrieve_batch_results(batch_id, apikey)
        
        if results:
            results_list = [result.model_dump() for result in results]
            return results_list, True
        else:
            # Ended but no results - unusual case
            error_info = [{
                "error": {
                    "message": "Batch ended but no results were returned",
                    "type": "no_results",
                    "batch_status": processing_status
                },
                "status_code": 400
            }]
            return error_info, True
    
    # Unknown status
    error_info = [{
        "error": {
            "message": f"Batch has unknown processing status: {processing_status}",
            "type": "unknown_status",
            "batch_status": processing_status
        },
        "status_code": 400
    }]
    return error_info, True
