from fastapi import HTTPException
from src.services.cache_service import store_in_cache
from src.services.utils.gcp_upload_service import uploadDoc
from google import genai
import tempfile
import os
import aiohttp
import asyncio
import time
from urllib.parse import urlparse
from config import Config
from src.configs.constant import redis_keys

async def image_processing(request):
    body = await request.form()
    file = body.get('image')
    
    file_content = await file.read()
    
    try:
        # Upload file using common GCP upload function
        image_url = await uploadDoc(
            file=file_content,
            folder='uploads',
            real_time=True,
            content_type=file.content_type,
            original_filename=file.filename
        )
        
        return {
            'success': True,
            'image_url': image_url
        }
    except Exception as e:
        # Handle exceptions and return an error response
        raise HTTPException(status_code=400, detail={"success": False, "error": "Error in image processing: "+ str(e)})


async def file_processing(request):
    # Check if request contains JSON data (for video URL) or form data (for file upload)
    content_type = request.headers.get('content-type', '')
    
    # Handle video URL upload (JSON request)
    if 'application/json' in content_type:
        body = await request.json()
        video_url = body.get('video_url')
        
        if video_url:
            return await _process_video_url(body)
        else:
            raise HTTPException(status_code=400, detail={"success": False, "error": "Video URL is required for JSON requests"})
    
    # Handle file upload (form data)
    else:
        body = await request.form()
        
        # Check for video_url in form data as well
        video_url = body.get('video_url')
        if video_url:
            # Convert form data to dict for video URL processing
            form_dict = {key: value for key, value in body.items()}
            return await _process_video_url(form_dict)
        
        # Check for both 'file' and 'video' in form data
        file = body.get('file') or body.get('video')
        
        if file is None:
            raise HTTPException(status_code=400, detail={"success": False, "error": "File or video_url not found"})
    
    # Extract thread parameters from form data
    thread_id = body.get('thread_id')
    sub_thread_id = body.get('sub_thread_id') 
    bridge_id = body.get('agent_id')
    
    file_content = await file.read()
    
    # Define non-previewable file extensions that should be rejected
    non_previewable_extensions = [
        '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.rar', '.tar', '.gz', '.7z',
        '.exe', '.dmg', '.apk', '.msi', '.deb', '.rpm',
        '.bin', '.iso', '.dll'
    ]
    
    # Check if file has a non-previewable extension
    if any(file.filename.lower().endswith(ext) for ext in non_previewable_extensions):
        raise HTTPException(
            status_code=400, 
            detail={
                "success": False, 
                "error": f"File type not supported. '{file.filename}' cannot be previewed in browser. Please upload images, PDFs, videos, or text files."
            }
        )

    # Check file type
    is_pdf = file.content_type == 'application/pdf' or file.filename.lower().endswith('.pdf')
    
    # Check for various video formats
    video_content_types = ['video/mp4', 'video/quicktime', 'video/avi', 'video/mov', 'video/webm', 'video/mkv']
    video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.qt']
    
    is_video = (file.content_type in video_content_types or 
                any(file.filename.lower().endswith(ext) for ext in video_extensions))
    
    try:
        
        # Handle video files with Gemini processing
        if is_video:
            # Get API key from form data - required for video processing
            api_key = body.get('apikey')
            if not api_key:
                raise HTTPException(status_code=400, detail={"success": False, "error": "API key is required for video processing"})
            
            # Create Gemini client
            gemini_client = genai.Client(api_key=api_key)
            
            # Create temporary file to upload to Gemini
            # Use original file extension or default to .mp4
            file_extension = os.path.splitext(file.filename)[1] if file.filename else '.mp4'
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                temp_file.write(file_content)
                temp_file_path = temp_file.name
            
            try:
                # Upload file to Gemini
                gemini_file = gemini_client.files.upload(file=temp_file_path)
                
                # Convert file object to dictionary for JSON serialization
                file_data = {
                    'name': gemini_file.name,
                    'display_name': gemini_file.display_name,
                    'mime_type': gemini_file.mime_type,
                    'size_bytes': gemini_file.size_bytes,
                    'create_time': gemini_file.create_time.isoformat() if gemini_file.create_time else None,
                    'expiration_time': gemini_file.expiration_time.isoformat() if gemini_file.expiration_time else None,
                    'update_time': gemini_file.update_time.isoformat() if gemini_file.update_time else None,
                    'sha256_hash': gemini_file.sha256_hash,
                    'uri': gemini_file.uri,
                    'download_uri': gemini_file.download_uri,
                    'state': str(gemini_file.state),
                    'source': str(gemini_file.source),
                    'video_metadata': gemini_file.video_metadata,
                    'error': gemini_file.error
                }
                
                return {
                    'success': True,
                    'file_data': file_data,
                    'message': 'Video uploaded to Gemini successfully'
                }
                
            finally:
                # Clean up temporary file
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
        
        # Handle regular files (non-video) with GCP upload
        else:
            # Upload file using common GCP upload function
            file_url = await uploadDoc(
                file=file_content,
                folder='uploads',
                real_time=True,
                content_type=file.content_type,
                original_filename=file.filename
            )

            # If PDF and thread parameters exist, save to Redis cache
            if is_pdf and thread_id and bridge_id:
                cache_key = f"{redis_keys['pdf_url_']}{bridge_id}_{thread_id}_{sub_thread_id or thread_id}"
                await store_in_cache(cache_key, [file_url], 604800)
            
            return {
                'success': True,
                'file_url': file_url
            }
            
    except Exception as e:
        # Handle exceptions and return an error response
        error_message = "Error in video processing: " if is_video else "Error in file processing: "
        raise HTTPException(status_code=400, detail={"success": False, "error": error_message + str(e)})


async def _process_video_url(body_dict):
    """Helper function to process video URL uploads"""
    video_url = body_dict.get('video_url')
    if not video_url:
        raise HTTPException(status_code=400, detail={"success": False, "error": "Video URL is required"})
    
    # Get API key from request body - required for video processing
    api_key = body_dict.get('api_key')
    if not api_key:
        raise HTTPException(status_code=400, detail={"success": False, "error": "API key is required for video processing"})
    
    # Validate URL format
    try:
        parsed_url = urlparse(video_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid video URL format"})
    except Exception:
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid video URL format"})
    
    # Check if URL points to a video file based on extension
    video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.qt']
    url_path = parsed_url.path.lower()
    is_video_url = any(url_path.endswith(ext) for ext in video_extensions)
    
    if not is_video_url:
        raise HTTPException(status_code=400, detail={"success": False, "error": "URL does not appear to point to a supported video file"})
    
    try:
        # Create Gemini client
        gemini_client = genai.Client(api_key=api_key)
        
        # Download video from URL and create temporary file
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url) as response:
                if response.status != 200:
                    raise HTTPException(status_code=400, detail={"success": False, "error": f"Failed to download video from URL. Status: {response.status}"})
                
                # Get file extension from URL or default to .mp4
                file_extension = os.path.splitext(parsed_url.path)[1] or '.mp4'
                
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
                if current_file.state.name == 'ACTIVE':
                    gemini_file = current_file  # Update with latest file info
                    break
                elif current_file.state.name == 'FAILED':
                    raise HTTPException(
                        status_code=500, 
                        detail={
                            "success": False, 
                            "error": f"File processing failed: {current_file.error}"
                        }
                    )
                
                # Check timeout
                elapsed_time = time.time() - start_time
                if elapsed_time > max_wait_time:
                    raise HTTPException(
                        status_code=408, 
                        detail={
                            "success": False, 
                            "error": f"File processing timeout after {max_wait_time} seconds. Current state: {current_file.state}"
                        }
                    )
                
                # Wait before next poll
                await asyncio.sleep(poll_interval)
            
            # Convert file object to dictionary for JSON serialization
            file_data = {
                'name': gemini_file.name,
                'display_name': gemini_file.display_name,
                'mime_type': gemini_file.mime_type,
                'size_bytes': gemini_file.size_bytes,
                'create_time': gemini_file.create_time.isoformat() if gemini_file.create_time else None,
                'expiration_time': gemini_file.expiration_time.isoformat() if gemini_file.expiration_time else None,
                'update_time': gemini_file.update_time.isoformat() if gemini_file.update_time else None,
                'sha256_hash': gemini_file.sha256_hash,
                'uri': gemini_file.uri,
                'download_uri': gemini_file.download_uri,
                'state': str(gemini_file.state),
                'source': str(gemini_file.source),
                'video_metadata': gemini_file.video_metadata,
                'error': gemini_file.error
            }
            
            return {
                'success': True,
                'file_data': file_data,
                'message': 'Video uploaded and processed successfully by Gemini',
                'original_url': video_url
            }
            
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
                
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=400, detail={"success": False, "error": f"Failed to download video from URL: {str(e)}"})
    except Exception as e:
        raise HTTPException(status_code=400, detail={"success": False, "error": f"Error in video URL processing: {str(e)}"})

