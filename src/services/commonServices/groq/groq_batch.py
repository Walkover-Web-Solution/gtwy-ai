import json
import uuid

from src.configs.constant import redis_keys
from src.db_services.conversationDbService import find_completed_batch_conversations
from src.controllers.conversationController import add_tool_call_data_in_history
from src.services.commonServices.createConversations import ConversationService
from src.configs.constant import service_name

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService
from .groq_run_batch import create_batch_file, process_batch_file
from globals import logger


class GroqBatch(BaseService):
    async def batch_execute(self):
        results = []
        message_mappings = []
        config_mappings = {}

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

        # Fetch thread history if thread_id is present (only completed conversations, not queued)
        thread_history = []
        if hasattr(self, 'thread_id') and self.thread_id:
            try:
                # Fetch only completed batch conversations (exclude queued ones)
                chats = await find_completed_batch_conversations(
                    org_id=self.org_id,
                    thread_id=self.thread_id,
                    sub_thread_id=getattr(self, 'sub_thread_id', self.thread_id),
                    bridge_id=self.bridge_id,
                    limit=3  # Fetch last 3 completed conversations
                )
                
                if chats:
                    # Add tool call data to history
                    chats = await add_tool_call_data_in_history(chats)
                    
                    # Convert to OpenAI conversation format (Groq uses same format)
                    memory = getattr(self, 'gpt_memory_context', None)
                    files = getattr(self, 'files', [])
                    
                    conversation_result = ConversationService.createOpenAiConversation(
                        conversation=chats,
                        memory=memory,
                        files=files
                    )
                    
                    if conversation_result.get('success'):
                        thread_history = conversation_result.get('messages', [])
                        logger.info(f"Loaded {len(thread_history)} history messages for Groq batch with thread_id={self.thread_id}")
                    else:
                        logger.warning(f"Failed to create conversation history for Groq batch: {conversation_result}")
                else:
                    logger.info(f"No completed conversation history found for thread_id={self.thread_id}")
                    
            except Exception as e:
                logger.error(f"Error fetching thread history for Groq batch: {str(e)}")
                # Continue without history if there's an error
                thread_history = []

        # Construct batch requests in OpenAI format (Groq is OpenAI-compatible)
        for idx, message in enumerate(self.batch):
            # Generate a unique message_id for each message
            # This will be sent as custom_id to Groq API (required by their format)
            message_id = str(uuid.uuid4())

            body_data = self.service_formatter(self.customConfig, service_name["groq"])

            # Construct OpenAI-compatible request with message_id as custom_id
            request_obj = {
                "custom_id": message_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body_data,
            }

            # Add processed system message first
            request_obj["body"]["messages"] = [{"role": "system", "content": self.processed_prompts[idx]}]

            # Add thread history after system prompt (if available)
            if thread_history:
                request_obj["body"]["messages"].extend(thread_history)

            # Add user message
            request_obj["body"]["messages"].append({"role": "user", "content": message})

            # Serialize to JSON string (one line per request for JSONL)
            results.append(json.dumps(request_obj))

            # Store message mapping for response
            mapping_item = {
                "message": message,
                "message_id": message_id
            }

            config_mappings[message_id] = request_obj["body"]
            
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
            "message_id_mapping": {item["message_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "model": self.model,
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "version_id": getattr(self, 'version_id', ''),
            "thread_id": self.thread_id,
            "meta": getattr(self, 'meta', None),
        }
        cache_key = f"{redis_keys['batch_']}{batch_file.id}"
        await store_in_cache(cache_key, batch_json, ttl=86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook within 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings,
            "ai_config_mapping": config_mappings,
        }
