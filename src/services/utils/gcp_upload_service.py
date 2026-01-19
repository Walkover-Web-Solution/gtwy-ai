import json
import uuid
import asyncio
from typing import Union, BinaryIO
from google.cloud import storage
from google.oauth2 import service_account
from config import Config
from src.services.utils.apiservice import fetch

async def uploadDoc(file: Union[bytes, str, BinaryIO], folder: str = 'uploads', real_time: bool = False, content_type: str = None, original_filename: str = None):
    """
    Common function to upload files to GCP storage
    
    Args:
        file: Can be:
            - bytes: File content as bytes
            - str: URL to fetch file from (for non real-time)
            - BinaryIO: File-like object
        folder: Folder name in GCP bucket (default: 'uploads')
        real_time: If True, upload immediately. If False, upload in background
        content_type: MIME type of the file
        original_filename: Original filename (used to preserve extension)
    
    Returns:
        str: GCP URL of uploaded file
    """
    filename = None  # Initialize filename to avoid UnboundLocalError
    try:
        # Setup GCP credentials and client
        credentials_dict = json.loads(Config.GCP_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(credentials_dict)
        storage_client = storage.Client(credentials=credentials)
        
        bucket = storage_client.bucket('resources.gtwy.ai')
        
        # Auto-generate filename based on context
        if isinstance(file, str):  # URL case - likely image
            filename = f"{folder}/{uuid.uuid4()}.png"
        elif original_filename:  # Has original filename - preserve extension
            extension = original_filename.split('.')[-1] if '.' in original_filename else ''
            filename = f"{folder}/{uuid.uuid4()}.{extension}" if extension else f"{folder}/{uuid.uuid4()}"
        elif content_type:  # Determine extension from content type
            if 'image' in content_type:
                filename = f"{folder}/{uuid.uuid4()}.png"
            elif 'pdf' in content_type:
                filename = f"{folder}/{uuid.uuid4()}.pdf"
            else:
                filename = f"{folder}/{uuid.uuid4()}"
        else:
            filename = f"{folder}/{uuid.uuid4()}"
        
        blob = bucket.blob(filename)
        gcp_url = f"https://resources.gtwy.ai/{filename}"
        
        if real_time:
            # Real-time upload - upload immediately and return URL
            if isinstance(file, str):
                # Fetch from URL first
                file_content, headers = await fetch(url=file, method='GET', image=True)
                blob.upload_from_file(file_content, content_type=content_type or 'application/octet-stream')
            elif isinstance(file, bytes):
                # Upload bytes directly
                blob.upload_from_string(file, content_type=content_type or 'application/octet-stream')
            else:
                # Upload from file-like object
                blob.upload_from_file(file, content_type=content_type or 'application/octet-stream')
            
            return gcp_url
        else:
            # Non real-time - start background upload and return URL immediately
            asyncio.create_task(_upload_background(file, blob, content_type, filename))
            return gcp_url
            
    except Exception as error:
        print(f"GCP upload failed for {filename}: {str(error)}")
        raise error

async def _upload_background(file: Union[bytes, str, BinaryIO], blob, content_type: str, filename: str):
    """
    Background task to upload file to GCP storage
    """
    try:
        if isinstance(file, str):
            # Fetch from URL
            file_content, headers = await fetch(url=file, method='GET', image=True)
            blob.upload_from_file(file_content, content_type=content_type or 'image/png')
        elif isinstance(file, bytes):
            # Upload bytes
            blob.upload_from_string(file, content_type=content_type or 'application/octet-stream')
        else:
            # Upload from file-like object
            blob.upload_from_file(file, content_type=content_type or 'application/octet-stream')
            
    except Exception as error:
        print(f"Background upload failed for {filename}: {str(error)}")
