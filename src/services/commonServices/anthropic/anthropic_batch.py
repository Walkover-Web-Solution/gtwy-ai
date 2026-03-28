import uuid

from src.configs.constant import redis_keys
from src.db_services.conversationDbService import find_completed_batch_conversations
from src.controllers.conversationController import add_tool_call_data_in_history
from src.services.commonServices.createConversations import ConversationService

from ...cache_service import store_in_cache
from ..baseService.baseService import BaseService
from .anthropic_run_batch import create_batch_requests
from globals import logger
from src.configs.constant import service_name



class AnthropicBatch(BaseService):
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
                    
                    # Convert to Anthropic conversation format
                    memory = getattr(self, 'gpt_memory_context', None)
                    files = getattr(self, 'files', [])
                    
                    conversation_result = await ConversationService.createAnthropicConversation(
                        conversation=chats,
                        memory=memory,
                        files=files
                    )
                    
                    if conversation_result.get('success'):
                        thread_history = conversation_result.get('messages', [])
                        logger.info(f"Loaded {len(thread_history)} history messages for Anthropic batch with thread_id={self.thread_id}")
                    else:
                        logger.warning(f"Failed to create conversation history for Anthropic batch: {conversation_result}")
                else:
                    logger.info(f"No completed conversation history found for thread_id={self.thread_id}")
                    
            except Exception as e:
                logger.error(f"Error fetching thread history for Anthropic batch: {str(e)}")
                # Continue without history if there's an error
                thread_history = []

        # Construct batch requests in Anthropic format
        for idx, message in enumerate(self.batch):
            # Generate a unique message_id for each message
            # This will be sent as custom_id to Anthropic API (required by their format)
            message_id = str(uuid.uuid4())

            # Build messages array with thread history + current message
            messages = []
            
            # Add thread history first (if available)
            if thread_history:
                messages.extend(thread_history)
            
            # Add current user message
            messages.append({"role": "user", "content": message})
            
            request_params = self.service_formatter(self.customConfig or {}, service_name["anthropic"])

            request_params["messages"] = messages
            request_params["system"] = self.processed_prompts[idx]

            # Create batch request entry with message_id sent as custom_id (required by Anthropic API)
            batch_entry = {
                "custom_id": message_id,
                "params": request_params
            }
            batch_requests.append(batch_entry)

            # Store message mapping for response
            mapping_item = {
                "message": message,
                "message_id": message_id
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
                "expired": message_batch.request_counts.expired,
            },
            "created_at": message_batch.created_at,
            "expires_at": message_batch.expires_at,
            "apikey": self.apikey,
            "webhook": self.webhook,
            "batch_variables": batch_variables,
            "message_id_mapping": {item["message_id"]: idx for idx, item in enumerate(message_mappings)},
            "service": self.service,
            "model": self.model,
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "version_id": getattr(self, 'version_id', ''),
            "thread_id": self.thread_id
        }
        cache_key = f"{redis_keys['batch_']}{message_batch.id}"
        await store_in_cache(cache_key, batch_json, ttl=86400)
        return {
            "success": True,
            "message": "Response will be successfully sent to the webhook within 24 hrs.",
            "batch_id": batch_id,
            "messages": message_mappings,
        }
