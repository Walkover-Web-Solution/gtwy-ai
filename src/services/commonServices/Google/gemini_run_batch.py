import json
import uuid
import tempfile
import os
from google import genai
from google.genai import types

async def create_batch_file(batch_requests, apiKey):
    """
    Creates a JSONL file and uploads it to Gemini File API.
    
    Args:
        batch_requests: List of JSON strings (JSONL entries)
        apiKey: Gemini API key
        
    Returns:
        Uploaded file object from Gemini File API
    """
    try:
        # Initialize Gemini client
        client = genai.Client(api_key=apiKey)
        
        # Create JSONL file content
        jsonl_content = "\n".join(batch_requests)
        
        # Create a temporary file to upload
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(jsonl_content)
            temp_file_path = temp_file.name
        
        try:
            # Upload the JSONL file to Gemini File API
            uploaded_file = client.files.upload(
                file=temp_file_path,
                config=types.UploadFileConfig(
                    display_name=f'batch-{uuid.uuid4()}',
                    mime_type='application/jsonl'
                )
            )
            return uploaded_file
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
    except Exception as e:
        print("Error in Gemini create_batch_file:", repr(e))
        print("Cause:", repr(getattr(e, "__cause__", None)))
        raise

async def process_batch_file(uploaded_file, apiKey, model):
    """
    Creates a batch job using the uploaded file.
    
    Args:
        uploaded_file: File object from create_batch_file
        apiKey: Gemini API key
        model: Model name to use for batch processing
        
    Returns:
        Batch job object
    """
    try:
        # Initialize Gemini client
        client = genai.Client(api_key=apiKey)
        
        # Create batch job with the uploaded file
        batch_job = client.batches.create(
            model=model,
            src=uploaded_file.name,
            config={
                'display_name': f'batch-job-{uuid.uuid4()}',
            }
        )
        print(f"Created batch job: {batch_job.name}")
        return batch_job
    except Exception as e:
        print(f"Error in Gemini process_batch_file: {e}")
        raise

async def retrieve_batch_status(batch_id, apiKey):
    """
    Retrieves the status of a batch job.
    
    Args:
        batch_id: Batch job name
        apiKey: Gemini API key
        
    Returns:
        Batch job object with current status
    """
    try:
        # Initialize Gemini client
        client = genai.Client(api_key=apiKey)
        
        # Get batch job status
        batch = client.batches.get(name=batch_id)
        print(f"Batch status: {batch.state}")
        return batch
    except Exception as e:
        print(f"Error in Gemini retrieve_batch_status: {e}")
        raise


async def download_gemini_file(file_uri, apikey):
    """
    Helper function to download and parse a Gemini batch result file.
    
    Args:
        file_uri: The file URI to download
        apikey: Gemini API key
        
    Returns:
        List of parsed JSON lines, or empty list if file doesn't exist or fails to parse
    """
    if not file_uri:
        return []
    
    try:
        client = genai.Client(api_key=apikey)
        file_content = client.files.get(name=file_uri).read()
        
        try:
            results = [json.loads(line) for line in file_content.decode('utf-8').splitlines() if line.strip()]
            return results
        except json.JSONDecodeError as e:
            print(f"JSON decoding error for file {file_uri}: {e}")
            return []
    except Exception as e:
        print(f"Error downloading file {file_uri}: {e}")
        return []


async def handle_batch_results(batch_id, apikey):
    """
    Handle Gemini batch processing - retrieve status and process results.
    
    Args:
        batch_id: Batch ID
        apikey: Gemini API key
        
    Returns:
        Tuple of (results, is_completed)
        - For succeeded batches: (results_list, True)
        - For failed/expired/cancelled with partial results: (combined_results, True)
        - For failed/expired/cancelled without results: (error_info, True)
        - For in-progress: (None, False) - continues polling
    """
    batch = await retrieve_batch_status(batch_id, apikey)
    state = batch.state
    
    # In-progress states - continue polling
    if state in [types.BatchState.STATE_PENDING, types.BatchState.STATE_RUNNING]:
        return None, False
    
    # Terminal states - download results
    output_uri = batch.output_uri
    
    # For Gemini, there's no separate error file like OpenAI
    # All results (success and errors) are in the output file
    output_results = await download_gemini_file(output_uri, apikey)
    
    if output_results:
        # We have some results (partial or complete)
        return output_results, True
    
    # No results available - return error based on state
    if state == types.BatchState.STATE_SUCCEEDED:
        # Succeeded but no output - unusual case
        error_info = [{
            "error": {
                "message": "Batch succeeded but no result file was generated",
                "type": "no_results",
                "batch_status": state.name
            },
            "status_code": 400
        }]
    elif state == types.BatchState.STATE_FAILED:
        error_info = [{
            "error": {
                "message": f"Batch failed. Error: {getattr(batch, 'error', 'No error details available')}",
                "type": "batch_failed",
                "batch_status": state.name
            },
            "status_code": 400
        }]
    elif state == types.BatchState.STATE_EXPIRED:
        error_info = [{
            "error": {
                "message": "Batch expired - running or pending for more than 48 hours and no partial results available",
                "type": "batch_expired",
                "batch_status": state.name
            },
            "status_code": 400
        }]
    elif state == types.BatchState.STATE_CANCELLED:
        error_info = [{
            "error": {
                "message": "Batch was cancelled and no partial results available",
                "type": "batch_cancelled",
                "batch_status": state.name
            },
            "status_code": 400
        }]
    else:
        # Unknown terminal state
        error_info = [{
            "error": {
                "message": f"Batch reached unknown terminal state: {state.name}",
                "type": "unknown_status",
                "batch_status": state.name
            },
            "status_code": 400
        }]
    
    return error_info, True
