import pydash as _
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from src.configs.constant import service_name
import json
import uuid
from ...cache_service import store_in_cache
from src.configs.constant import redis_keys
from src.services.commonServices.anthropic.anthropicCall import Anthropic
from .anthropic_run_batch import create_batch_requests

class AnthropicBatch(BaseService):
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
        
        # Construct batch requests in Anthropic format
        for idx, message in enumerate(self.batch):
            # Generate a unique custom_id for each request
            custom_id = str(uuid.uuid4())

            # Construct Anthropic message format
            request_params = {
                "model": self.model,
                "max_tokens": self.customConfig.get("max_tokens", 1024),
                "messages": [
                    {
                        "role": "user",
                        "content": message
                    }
                ]
            }
            
            # Add processed system prompt
            request_params["system"] = self.processed_prompts[idx]
            
            # Add other config from customConfig
            if self.customConfig:
                # Add temperature, top_p, etc.
                for key in ['temperature', 'top_p', 'top_k', 'stop_sequences']:
                    if key in self.customConfig:
                        request_params[key] = self.customConfig[key]

            # Create batch request entry with custom_id and params
            batch_entry = {
                "custom_id": custom_id,
                "params": request_params
            }
            batch_requests.append(batch_entry)
            
            # Store message mapping for response
            mapping_item = {
                "message": message,
                "custom_id": custom_id
            }
            
            # Add batch_variables to mapping if provided
            if batch_variables is not None:
                mapping_item["variables"] = batch_variables[idx]
            
            message_mappings.append(mapping_item)

        # Create batch using Anthropic API
        message_batch = await create_batch_requests(batch_requests, self.apikey, self.model)
        
        batch_id = message_batch.id
        batch_json = {
            "id": message_batch.id,
            "processing_status": message_batch.processing_status,
            "request_counts": {
                "processing": message_batch.request_counts.processing,
                "succeeded": message_batch.request_counts.succeeded,
                "errored": message_batch.request_counts.errored,
                "canceled": message_batch.request_counts.canceled,
                "expired": message_batch.request_counts.expired
            },
            "created_at": message_batch.created_at,
            "expires_at": message_batch.expires_at,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "custom_id_mapping": {item["custom_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "model": self.model
        }
        cache_key = f"{redis_keys['batch_']}{message_batch.id}"
        await store_in_cache(cache_key, batch_json, ttl = 86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook within 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings
        }
