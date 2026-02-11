import json
import uuid

from src.configs.constant import redis_keys
from src.services.commonServices.Mistral.mistral_run_batch import create_batch_file, process_batch_file

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService


class MistralBatch(BaseService):
    async def batch_execute(self):
        batch_requests = []
        message_mappings = []

        # Validate batch_variables if provided
        batch_variables = self.batch_variables if hasattr(self, "batch_variables") and self.batch_variables else None
        if batch_variables is not None:
            if not isinstance(batch_variables, list):
                return {"success": False, "message": "batch_variables must be an array"}
            if len(batch_variables) != len(self.batch):
                return {
                    "success": False,
                    "message": f"batch_variables array length ({len(batch_variables)}) must match batch array length ({len(self.batch)})",
                }

        # Construct batch requests in Mistral JSONL format
        for idx, message in enumerate(self.batch):
            # Generate a unique message_id for each message
            # This will be sent as custom_id to Mistral API (required by their format)
            message_id = str(uuid.uuid4())

            # Construct Mistral batch request body
            request_body = {"messages": [], "max_tokens": self.customConfig.get("max_tokens", 1024)}

            # Add processed system message
            request_body["messages"].append({"role": "system", "content": self.processed_prompts[idx]})

            # Add user message
            request_body["messages"].append({"role": "user", "content": message})

            # Add other config from customConfig
            if self.customConfig:
                for key in ["temperature", "top_p", "random_seed", "safe_prompt"]:
                    if key in self.customConfig:
                        request_body[key] = self.customConfig[key]

            # Create JSONL entry with message_id sent as custom_id (required by Mistral API)
            batch_entry = {
                "custom_id": message_id,
                "body": request_body
            }
            batch_requests.append(json.dumps(batch_entry))

            # Store message mapping for response
            mapping_item = {
                "message": message,
                "message_id": message_id
            }
            
            # Add batch_variables to mapping if provided
            if batch_variables is not None:
                mapping_item["variables"] = batch_variables[idx]

            message_mappings.append(mapping_item)

        # Upload batch file and create batch job
        uploaded_file = await create_batch_file(batch_requests, self.apikey)
        batch_job = await process_batch_file(uploaded_file, self.apikey, self.model)

        batch_id = batch_job.id
        batch_json = {
            "id": batch_job.id,
            "status": batch_job.status,
            "created_at": batch_job.created_at,
            "model": self.model,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "message_id_mapping": {item["message_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "uploaded_file_id": uploaded_file.id,
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "version_id": getattr(self, 'version_id', ''),
            "thread_id": self.thread_id
        }
        cache_key = f"{redis_keys['batch_']}{batch_job.id}"
        await store_in_cache(cache_key, batch_json, ttl=86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook within 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings,
        }
