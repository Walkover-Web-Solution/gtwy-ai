import asyncio
import os
import tempfile
import time
from urllib.parse import urlparse

import aiohttp
from fastapi import HTTPException, UploadFile
from google import genai

from src.configs.constant import redis_keys
from src.schemas.image_schemas import FileUploadRequest, VideoUrlUploadRequest
from src.services.cache_service import store_in_cache
from src.services.utils.gcp_upload_service import uploadDoc


async def image_processing(file: UploadFile):
    file_content = await file.read()

    try:
        # Upload file using common GCP upload function
        image_url = await uploadDoc(
            file=file_content,
            folder="uploads",
            real_time=True,
            content_type=file.content_type,
            original_filename=file.filename,
        )

        return {"success": True, "image_url": image_url}
    except Exception as e:
        # Handle exceptions and return an error response
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Error in image processing: " + str(e)}
        ) from e


async def file_processing(request):
    # Check if request contains JSON data (for video URL) or form data (for file upload)
    content_type = request.headers.get("content-type", "")

    # Handle video URL upload (JSON request)
    if "application/json" in content_type:
        body = await request.json()
        vid_req = VideoUrlUploadRequest.model_validate(body)
        return await _process_video_url(video_url=str(vid_req.video_url), api_key=vid_req.apikey or "")

    # Handle file upload (form data)
    else:
        body = await request.form()

        # Check for video_url in form data as well
        video_url = body.get("video_url")
        if video_url:
            vid_req = VideoUrlUploadRequest.model_validate({
                "video_url": video_url,
                "apikey": body.get("apikey"),
            })
            return await _process_video_url(video_url=str(vid_req.video_url), api_key=vid_req.apikey or "")

        # Check for both 'file' and 'video' in form data
        file = body.get("file") or body.get("video")

    # Extract thread parameters from form data
    thread_id = body.get("thread_id")
    sub_thread_id = body.get("sub_thread_id")
    bridge_id = body.get("agent_id")

    upload = FileUploadRequest.model_validate({"file": file, "apikey": body.get("apikey") or ""})
    file_content = await file.read()
    is_pdf = upload.is_pdf
    is_video = upload.is_video
    api_key = upload.apikey or ""

    try:
        # Handle video files with Gemini processing
        if is_video:
            # Create Gemini client
            gemini_client = genai.Client(api_key=api_key)

            # Create temporary file to upload to Gemini
            # Use original file extension or default to .mp4
            file_extension = os.path.splitext(file.filename)[1] if file.filename else ".mp4"
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                temp_file.write(file_content)
                temp_file_path = temp_file.name

            try:
                # Upload file to Gemini
                gemini_file = gemini_client.files.upload(file=temp_file_path)

                # Convert file object to dictionary for JSON serialization
                file_data = {
                    "name": gemini_file.name,
                    "display_name": gemini_file.display_name,
                    "mime_type": gemini_file.mime_type,
                    "size_bytes": gemini_file.size_bytes,
                    "create_time": gemini_file.create_time.isoformat() if gemini_file.create_time else None,
                    "expiration_time": gemini_file.expiration_time.isoformat() if gemini_file.expiration_time else None,
                    "update_time": gemini_file.update_time.isoformat() if gemini_file.update_time else None,
                    "sha256_hash": gemini_file.sha256_hash,
                    "uri": gemini_file.uri,
                    "download_uri": gemini_file.download_uri,
                    "state": str(gemini_file.state),
                    "source": str(gemini_file.source),
                    "video_metadata": gemini_file.video_metadata,
                    "error": gemini_file.error,
                }

                return {"success": True, "file_data": file_data, "message": "Video uploaded to Gemini successfully"}

            finally:
                # Clean up temporary file
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)

        # Handle regular files (non-video) with GCP upload
        else:
            # Upload file using common GCP upload function
            file_url = await uploadDoc(
                file=file_content,
                folder="uploads",
                real_time=True,
                content_type=file.content_type,
                original_filename=file.filename,
            )

            # If PDF and thread parameters exist, save to Redis cache
            if is_pdf and thread_id and bridge_id:
                cache_key = f"{redis_keys['pdf_url_']}{bridge_id}_{thread_id}_{sub_thread_id or thread_id}"
                await store_in_cache(cache_key, [file_url], 604800)

            return {"success": True, "file_url": file_url}

    except Exception as e:
        # Handle exceptions and return an error response
        error_message = "Error in video processing: " if is_video else "Error in file processing: "
        raise HTTPException(status_code=400, detail={"success": False, "error": error_message + str(e)}) from e


async def _process_video_url(video_url: str, api_key: str):
    """Helper function to process video URL uploads"""
    try:
        # Create Gemini client
        gemini_client = genai.Client(api_key=api_key)

        # Download video from URL and create temporary file
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "success": False,
                            "error": f"Failed to download video from URL. Status: {response.status}",
                        },
                    )

                # Get file extension from URL or default to .mp4
                file_extension = os.path.splitext(urlparse(video_url).path)[1] or ".mp4"

                # Create temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                    async for chunk in response.content.iter_chunked(8192):
                        temp_file.write(chunk)
                    temp_file_path = temp_file.name

        try:
            # Upload file to Gemini
            gemini_file = gemini_client.files.upload(file=temp_file_path)

            # Wait for file processing to complete
            max_wait_time = 300  # 5 minutes timeout
            poll_interval = 0.1  # Check every 100 milliseconds
            start_time = time.time()

            while True:
                # Get current file status
                current_file = gemini_client.files.get(name=gemini_file.name)
                print(f"File state: {current_file.state}")

                # Check if processing is complete
                if current_file.state.name == "ACTIVE":
                    gemini_file = current_file  # Update with latest file info
                    break
                elif current_file.state.name == "FAILED":
                    raise HTTPException(
                        status_code=500,
                        detail={"success": False, "error": f"File processing failed: {current_file.error}"},
                    )

                # Check timeout
                elapsed_time = time.time() - start_time
                if elapsed_time > max_wait_time:
                    raise HTTPException(
                        status_code=408,
                        detail={
                            "success": False,
                            "error": f"File processing timeout after {max_wait_time} seconds. Current state: {current_file.state}",
                        },
                    )

                # Wait before next poll
                await asyncio.sleep(poll_interval)

            # Convert file object to dictionary for JSON serialization
            file_data = {
                "name": gemini_file.name,
                "display_name": gemini_file.display_name,
                "mime_type": gemini_file.mime_type,
                "size_bytes": gemini_file.size_bytes,
                "create_time": gemini_file.create_time.isoformat() if gemini_file.create_time else None,
                "expiration_time": gemini_file.expiration_time.isoformat() if gemini_file.expiration_time else None,
                "update_time": gemini_file.update_time.isoformat() if gemini_file.update_time else None,
                "sha256_hash": gemini_file.sha256_hash,
                "uri": gemini_file.uri,
                "download_uri": gemini_file.download_uri,
                "state": str(gemini_file.state),
                "source": str(gemini_file.source),
                "video_metadata": gemini_file.video_metadata,
                "error": gemini_file.error,
            }

            return {
                "success": True,
                "file_data": file_data,
                "message": "Video uploaded and processed successfully by Gemini",
                "original_url": video_url,
            }

        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    except aiohttp.ClientError as e:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": f"Failed to download video from URL: {str(e)}"}
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": f"Error in video URL processing: {str(e)}"}
        ) from e
