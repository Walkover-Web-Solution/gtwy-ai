import pydash as _
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from src.configs.constant import service_name
import json
import uuid
from ...cache_service import store_in_cache
from src.configs.constant import redis_keys
from .groq_run_batch import create_batch_file, process_batch_file


class GroqBatch(BaseService):
    async def batch_execute(self):
        results = []
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
        
        # Construct batch requests in OpenAI format (Groq is OpenAI-compatible)
        for idx, message in enumerate(self.batch):
            # Generate a unique ID for each request
            custom_id = str(uuid.uuid4())

            # Construct OpenAI-compatible request
            request_obj = {
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self.model,
                    "messages": []
                }
            }
            
            # Add processed system message 
            request_obj["body"]["messages"].append({
                "role": "system",
                "content": self.processed_prompts[idx]
            })
            
            # Add user message
            request_obj["body"]["messages"].append({
                "role": "user",
                "content": message
            })
            
            # Add other config from customConfig (like temperature, max_tokens, etc.)
            if self.customConfig:
                for key, value in self.customConfig.items():
                    if key not in ['messages', 'prompt', 'model']:
                        request_obj["body"][key] = value

            # Serialize to JSON string (one line per request for JSONL)
            results.append(json.dumps(request_obj))
            
            # Store message mapping for response
            mapping_item = {
                "message": message,
                "custom_id": custom_id
            }
            
            # Add batch_variables to mapping if provided
            if batch_variables is not None:
                mapping_item["variables"] = batch_variables[idx]
            
            message_mappings.append(mapping_item)

        # Upload batch file and create batch job using Groq's native library
        batch_input_file = await create_batch_file(results, self.apikey)
        batch_file = await process_batch_file(batch_input_file, self.apikey)
        
        batch_id = batch_file.id
        batch_json = {
            "id": batch_file.id,
            "status": batch_file.status,
            "created_at": batch_file.created_at,
            "model": self.model,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "custom_id_mapping": {item["custom_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service
        }
        cache_key = f"{redis_keys['batch_']}{batch_file.id}"
        await store_in_cache(cache_key, batch_json, ttl = 86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook within 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings
        }
