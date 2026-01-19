import pydash as _
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from src.configs.constant import service_name
import json
import uuid
from ...cache_service import store_in_cache
from src.configs.constant import redis_keys
from src.services.commonServices.Google.gemini_run_batch import create_batch_file, process_batch_file


class GeminiBatch(BaseService):
    async def batch_execute(self):
        batch_requests = []
        message_mappings = []
        
        # Validate batch_variables if provided
        batch_variables = self.batch_variables if hasattr(self, 'batch_variables') and self.batch_variables else None
        if batch_variables is not None:
            if not isinstance(batch_variables, list):
                return {
                    "success": False,
                    "message": "batch_variables must be an array"
                }
            if len(batch_variables) != len(self.batch):
                return {
                    "success": False,
                    "message": f"batch_variables array length ({len(batch_variables)}) must match batch array length ({len(self.batch)})"
                }
        
        # Construct batch requests in Gemini JSONL format
        for idx, message in enumerate(self.batch):
            # Generate a unique key for each request
            custom_id = str(uuid.uuid4())

            # Construct Gemini native format request
            request_content = {
                'contents': [
                    {
                        'parts': [
                            {'text': message}
                        ]
                    }
                ]
            }
            
            # Add processed system instruction 
            request_content['config'] = {
                'system_instruction': {
                    'parts': [{'text': self.processed_prompts[idx]}]
                }
            }
            
            # Add other config from customConfig (like temperature, max_tokens, etc.)
            if self.customConfig:
                if 'config' not in request_content:
                    request_content['config'] = {}
                # Merge customConfig into config, excluding any messages/prompt fields
                for key, value in self.customConfig.items():
                    if key not in ['messages', 'prompt', 'model']:
                        request_content['config'][key] = value

            # Create JSONL entry with key and request
            batch_entry = {
                "key": custom_id,
                "request": request_content
            }
            batch_requests.append(json.dumps(batch_entry))
            
            # Store message mapping for response
            mapping_item = {
                "message": message,
                "custom_id": custom_id
            }
            
            # Add batch_variables to mapping if provided
            if batch_variables is not None:
                mapping_item["variables"] = batch_variables[idx]
            
            message_mappings.append(mapping_item)

        # Upload batch file and create batch job
        uploaded_file = await create_batch_file(batch_requests, self.apikey)
        batch_job = await process_batch_file(uploaded_file, self.apikey, self.model)
        
        batch_id = batch_job.name
        batch_json = {
            "id": batch_job.name,
            "state": batch_job.state,
            "create_time": batch_job.create_time,
            "model": batch_job.model or self.model,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "custom_id_mapping": {item["custom_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "uploaded_file": uploaded_file.name
        }
        cache_key = f"{redis_keys['batch_']}{batch_job.name}"
        await store_in_cache(cache_key, batch_json, ttl = 86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook wihtin 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings
        }
