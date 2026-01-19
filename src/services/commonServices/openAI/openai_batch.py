import json
import uuid

from src.configs.constant import redis_keys
from src.services.commonServices.openAI.openai_run_batch import create_batch_file, process_batch_file

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService


class OpenaiBatch(BaseService):
    async def batch_execute(self):
        results = []
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

        # Assume "self.batch" is the list of messages we want to process
        for idx, message in enumerate(self.batch):
            # Copy all keys from self.customConfig into the body
            body_data = self.customConfig

            # Generate a unique ID for each request
            custom_id = str(uuid.uuid4())

            # Add messages array with processed system prompt and user message
            body_data["messages"] = [
                {"role": "system", "content": self.processed_prompts[idx]},
                {"role": "user", "content": message},
            ]

            # Construct one JSONL line for each message
            request_obj = {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body_data}

            # Serialize to JSON string
            results.append(json.dumps(request_obj))

            # Store message mapping for response
            mapping_item = {"message": message, "custom_id": custom_id}

            # Add batch_variables to mapping if provided
            if batch_variables is not None:
                mapping_item["variables"] = batch_variables[idx]

            message_mappings.append(mapping_item)

        batch_input_file = await create_batch_file(results, self.apikey)
        batch_file = await process_batch_file(batch_input_file, self.apikey)
        batch_id = batch_file.id
        batch_json = {
            "id": batch_file.id,
            "completion_window": batch_file.completion_window,
            "created_at": batch_file.created_at,
            "endpoint": batch_file.endpoint,
            "input_file_id": batch_file.input_file_id,
            "object": batch_file.object,
            "status": batch_file.status,
            "cancelled_at": batch_file.cancelled_at,
            "cancelling_at": batch_file.cancelling_at,
            "completed_at": batch_file.completed_at,
            "error_file_id": batch_file.error_file_id,
            "errors": batch_file.errors,
            "expires_at": batch_file.expires_at,
            "metadata": batch_file.metadata,
            "request_counts": {
                "completed": batch_file.request_counts.completed,
                "failed": batch_file.request_counts.failed,
                "total": batch_file.request_counts.total,
            },
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "custom_id_mapping": {item["custom_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
        }
        cache_key = f"{redis_keys['batch_']}{batch_file.id}"
        await store_in_cache(cache_key, batch_json, ttl=86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook wihtin 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings,
        }
