import json
import uuid

from src.configs.constant import redis_keys
from src.services.commonServices.Google.gemini_run_batch import create_batch_file, process_batch_file
from src.db_services.conversationDbService import find_completed_batch_conversations
from src.controllers.conversationController import add_tool_call_data_in_history
from src.services.commonServices.createConversations import ConversationService

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService
from globals import logger

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService


class GeminiBatch(BaseService):
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
                    
                    # Convert to Gemini conversation format
                    memory = getattr(self, 'gpt_memory_context', None)
                    
                    conversation_result = ConversationService.createGeminiConversation(
                        conversation=chats,
                        memory=memory
                    )
                    
                    if conversation_result.get('success'):
                        gemini_history = conversation_result.get('messages', [])
                        # Convert to Gemini's native format for batch API
                        for msg in gemini_history:
                            role = "user" if msg["role"] == "user" else "model"
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                thread_history.append({"role": role, "parts": [{"text": content}]})
                            elif isinstance(content, list):
                                parts = []
                                for part in content:
                                    if part.get("type") == "text":
                                        parts.append({"text": part.get("text", "")})
                                    elif part.get("type") == "image_url":
                                        # Gemini image format - would need special handling
                                        pass
                                if parts:
                                    thread_history.append({"role": role, "parts": parts})
                        
                        logger.info(f"Loaded {len(thread_history)} history messages for Gemini batch with thread_id={self.thread_id}")
                    else:
                        logger.warning(f"Failed to create conversation history for Gemini batch: {conversation_result}")
                else:
                    logger.info(f"No completed conversation history found for thread_id={self.thread_id}")
                    
            except Exception as e:
                logger.error(f"Error fetching thread history for Gemini batch: {str(e)}")
                # Continue without history if there's an error
                thread_history = []

        # Construct batch requests in Gemini JSONL format
        for idx, message in enumerate(self.batch):
            # Generate a unique message_id for each message
            # This will be sent as key to Gemini API (required by their format)
            message_id = str(uuid.uuid4())

            # Construct Gemini native format request with history
            contents = []
            
            # Add thread history first (if available)
            if thread_history:
                contents.extend(thread_history)
            
            # Add current user message
            contents.append({"parts": [{"text": message}]})
            
            request_content = {"contents": contents}

            # Add processed system instruction
            request_content["config"] = {"system_instruction": {"parts": [{"text": self.processed_prompts[idx]}]}}

            # Add other config from customConfig (like temperature, max_tokens, etc.)
            if self.customConfig:
                if "config" not in request_content:
                    request_content["config"] = {}
                # Merge customConfig into config, excluding any messages/prompt fields
                for key, value in self.customConfig.items():
                    if key not in ["messages", "prompt", "model"]:
                        request_content["config"][key] = value

            # Create JSONL entry with message_id sent as key (required by Gemini API)
            batch_entry = {
                "key": message_id,
                "request": request_content
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

        batch_id = batch_job.name
        batch_json = {
            "id": batch_job.name,
            "state": batch_job.state,
            "create_time": batch_job.create_time,
            "model": batch_job.model or self.model,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "message_id_mapping": {item["message_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "uploaded_file": uploaded_file.name,
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "version_id": getattr(self, 'version_id', ''),
            "thread_id": self.thread_id
        }
        cache_key = f"{redis_keys['batch_']}{batch_job.name}"
        await store_in_cache(cache_key, batch_json, ttl=86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook wihtin 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings,
        }
