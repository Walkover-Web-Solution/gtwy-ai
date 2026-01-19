import pydash as _
from ..baseService.baseService import BaseService
from src.configs.constant import service_name
from src.configs.model_configuration import model_config_document
from ..createConversations import ConversationService
from ....services.utils.apiservice import  fetch_images_b64
from src.services.utils.ai_middleware_format import Response_formatter


class Anthropic(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        conversation = []
        images_input = []
        conversation = (await ConversationService.createAnthropicConversation(self.configuration.get('conversation'), self.memory, self.files)).get('messages', [])        
        self.customConfig['system'] = self.configuration.get('prompt')
        if self.image_data:
            images_data = await fetch_images_b64(self.image_data)
            images_input = [{'type': 'image', 'source': {'type': 'base64', 'media_type': image_media_type, 'data': image_data}} for image_data, image_media_type in images_data]
        elif self.files and len(self.files) > 0:
            # Handle files (documents) when no images
            file_content = [{'type': 'document', 'source': {'type': 'url', 'url': file_url}} for file_url in self.files]
            content = file_content + [{"type": "text", "text": self.user}] if self.user else file_content
            self.customConfig["messages"] = conversation + [{"role": "user", "content": content}]
        
        # Original image handling logic (only runs if no files or images_input is populated)
        if not (self.files and len(self.files) > 0):
            self.customConfig["messages"] = conversation + [{"role": "user", "content": (images_input + [{"type": "text", "text": self.user}] if self.user else images_input)}] if images_input or self.user else conversation
        self.customConfig['tools'] = self.tool_call if self.tool_call and len(self.tool_call) != 0 else []
        
        # Add web search support for Anthropic
        self.customConfig = self.service_formatter(self.customConfig, service_name['anthropic'])
        if len(self.built_in_tools) > 0:
            if 'web_search' in self.built_in_tools and 'tools' in model_config_document[self.service][self.model]['configuration']:
                if 'tools' not in self.customConfig or self.customConfig['tools'] is None:
                    self.customConfig['tools'] = []
                
                # Use Anthropic's official web search format
                web_search_tool = {
                    "type": "web_search_20250305",
                    "name": "web_search"
                }
                
                # Add allowed domains filtering if provided
                if self.web_search_filters and isinstance(self.web_search_filters, list):
                    web_search_tool["allowed_domains"] = self.web_search_filters
                
                self.customConfig['tools'].append(web_search_tool)
        
        antrophic_response = await self.chats(self.customConfig, self.apikey, service_name['anthropic'])
        modelResponse = antrophic_response.get("modelResponse", {})
        
        if not antrophic_response.get('success'):
            if not self.playground:
                await self.handle_failure(antrophic_response)
            raise ValueError(antrophic_response.get('error'))
        functionCallRes = await self.function_call(self.customConfig, service_name['anthropic'], antrophic_response, 0, {})
        if not functionCallRes.get('success'):
            await self.handle_failure(functionCallRes)
            raise ValueError(functionCallRes.get('error'))
        
        self.update_model_response(modelResponse, functionCallRes)
        tools = functionCallRes.get("tools", {})
        response = await Response_formatter(modelResponse, service_name['anthropic'], tools, self.type, self.image_data)
        if not self.playground:
            transfer_config = functionCallRes.get('transfer_agent_config') if functionCallRes else None
            historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)
        # Add transfer_agent_config to return if transfer was detected
        result = {'success': True, 'modelResponse': modelResponse, 'historyParams': historyParams, 'response': response}
        if functionCallRes.get('transfer_agent_config'):
            result['transfer_agent_config'] = functionCallRes['transfer_agent_config']
        return result
