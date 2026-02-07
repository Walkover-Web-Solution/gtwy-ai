import io
import json

import certifi
import httpx
from openai import AsyncOpenAI


async def create_batch_file(data, apiKey):
    try:
        file_content = "\n".join(data)
        filelike_obj = io.BytesIO(file_content.encode("utf-8"))
        filelike_obj.name = "batch.jsonl"  # important for multipart metadata
        filelike_obj.seek(0)

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)

        timeout = httpx.Timeout(60.0, connect=10.0)

        http_client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            verify=certifi.where(),
            transport=httpx.AsyncHTTPTransport(retries=3),
        )

        try:
            openAI = AsyncOpenAI(api_key=apiKey, http_client=http_client)

            batch_input_file = await openAI.files.create(file=filelike_obj, purpose="batch")
            return batch_input_file
        finally:
            await http_client.aclose()
    except Exception as e:
        print("Error in OpenAI create_batch_file:", repr(e))
        print("Cause:", repr(getattr(e, "__cause__", None)))
        raise


async def process_batch_file(batch_input_file, apiKey):
    try:
        batch_input_file_id = batch_input_file.id

        # Create httpx client with proper production configuration
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)

        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=httpx.AsyncHTTPTransport(retries=3, verify=certifi.where()),
            limits=limits,
            follow_redirects=True,
        )

        try:
            openAI = AsyncOpenAI(api_key=apiKey, http_client=http_client)

            result = await openAI.batches.create(
                input_file_id=batch_input_file_id, endpoint="/v1/chat/completions", completion_window="24h"
            )
            print(result)
            return result
        finally:
            await http_client.aclose()
    except Exception as e:
        print(f"Error in OpenAI process_batch_file: {e}")
        raise


async def retrieve_batch_status(batch_id, apiKey):
    try:
        # Create httpx client with proper production configuration
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)

        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=httpx.AsyncHTTPTransport(retries=3, verify=certifi.where()),
            limits=limits,
            follow_redirects=True,
        )

        try:
            openAI = AsyncOpenAI(api_key=apiKey, http_client=http_client)
            batch = await openAI.batches.retrieve(batch_id)
            print(batch)
            return batch
        finally:
            await http_client.aclose()
    except Exception as e:
        print(f"Error in OpenAI retrieve_batch_status: {e}")
        raise


async def download_batch_file(file_id, apikey):
    """
    Helper function to download and parse a batch result file.

    Args:
        file_id: The file ID to download
        apikey: OpenAI API key

    Returns:
        List of parsed JSON lines, or empty list if file doesn't exist or fails to parse
    """
    if not file_id:
        return []

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        transport=httpx.AsyncHTTPTransport(retries=3, verify=certifi.where()),
        limits=limits,
        follow_redirects=True,
    )

    try:
        import asyncio

        openAI = AsyncOpenAI(api_key=apikey, http_client=http_client)

        file_response = await openAI.files.content(file_id)
        file_content = await asyncio.to_thread(file_response.read)

        try:
            results = [json.loads(line) for line in file_content.decode("utf-8").splitlines() if line.strip()]
            return results
        except json.JSONDecodeError as e:
            print(f"JSON decoding error for file {file_id}: {e}")
            return []
    except Exception as e:
        print(f"Error downloading file {file_id}: {e}")
        return []
    finally:
        await http_client.aclose()


async def handle_batch_results(batch_id, apikey):
    """
    Handle OpenAI batch processing - retrieve status and process results.

    Args:
        batch_id: Batch ID
        apikey: OpenAI API key

    Returns:
        Tuple of (results, is_completed)
        - For completed batches: (results_list, True)
        - For failed/expired/cancelled with partial results: (combined_results, True)
        - For failed/expired/cancelled without results: (error_info, True)
        - For in-progress: (None, False) - continues polling
    """
    batch = await retrieve_batch_status(batch_id, apikey)
    status = batch.status

    # In-progress states - continue polling
    if status in ["validating", "in_progress", "finalizing", "cancelling"]:
        return None, False

    # Terminal states - download results (both success and error files)
    output_file_id = batch.output_file_id
    error_file_id = batch.error_file_id

    # Download both output and error files in parallel
    import asyncio

    output_results, error_results = await asyncio.gather(
        download_batch_file(output_file_id, apikey), download_batch_file(error_file_id, apikey)
    )

    # Combine results
    all_results = output_results + error_results

    if all_results:
        # We have some results (partial or complete)
        return all_results, True

    # No results available - return error based on status
    if status == "completed":
        # Completed but no files - unusual case
        error_info = [
            {
                "error": {
                    "message": "Batch completed but no result files were generated",
                    "type": "no_results",
                    "batch_status": status,
                },
                "status_code": 400,
            }
        ]
    elif status == "failed":
        error_info = [
            {
                "error": {
                    "message": f"Batch failed validation or processing. Errors: {getattr(batch, 'errors', 'No error details available')}",
                    "type": "batch_failed",
                    "batch_status": status,
                },
                "status_code": 400,
            }
        ]
    elif status == "expired":
        error_info = [
            {
                "error": {
                    "message": "Batch expired - not completed within 24-hour window and no partial results available",
                    "type": "batch_expired",
                    "batch_status": status,
                },
                "status_code": 400,
            }
        ]
    elif status == "cancelled":
        error_info = [
            {
                "error": {
                    "message": "Batch was cancelled and no partial results available",
                    "type": "batch_cancelled",
                    "batch_status": status,
                },
                "status_code": 400,
            }
        ]
    else:
        # Unknown terminal status
        error_info = [
            {
                "error": {
                    "message": f"Batch reached unknown terminal status: {status}",
                    "type": "unknown_status",
                    "batch_status": status,
                },
                "status_code": 400,
            }
        ]

    return error_info, True
