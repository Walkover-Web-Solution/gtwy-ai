import pydash as _
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter

class Mistral(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == 'image':
            self.customConfig['prompt'] = self.user
            openAIResponse = await self.image(self.customConfig, self.apikey, service_name['openai'])
            modelResponse = openAIResponse.get("modelResponse", {})
            if not openAIResponse.get('success'):
                if not self.playground:
                    await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get('error'))
            if not self.playground:
                historyParams = self.prepare_history_params(modelResponse, tools)
                historyParams['message'] = "image generated successfully"
                historyParams['type'] = 'assistant'
        else:
            conversation = ConversationService.create_mistral_ai_conversation(self.configuration.get('conversation'), self.memory).get('messages', [])
            
            if self.reasoning_model:
                self.customConfig["messages"] =  conversation + ([{"role": "user", "content": self.user}] if self.user else []) 
            else:
                # Check if we have any multimodal content (images or audio)
                has_multimodal = self.image_data or self.audio_data
                
                if not has_multimodal:
                    self.customConfig["messages"] = [ {"role": "system", "content": self.configuration['prompt']}] + conversation + ([{"role": "user", "content": self.user}] if self.user else []) 
                else:
                    self.customConfig["messages"] = [{"role": "system", "content": self.configuration['prompt']}] + conversation
                    user_content = []
                    
                    # Add images if present
                    if self.image_data and isinstance(self.image_data, list):
                        for image_url in self.image_data:
                            user_content.append({"type": "image_url", "image_url": {"url": image_url}})
                    
                    # Add audio if present 
                    if self.audio_data and isinstance(self.audio_data, list):
                        for audio_url in self.audio_data:
                            user_content.append({"type": "input_audio", "input_audio": audio_url})
                    
                    # Add user text at the end
                    if self.user:
                        user_content.append({"type": "text", "text": self.user})
                    
                    self.customConfig["messages"].append({'role': 'user', 'content': user_content})
                
                self.customConfig =self.service_formatter(self.customConfig, service_name['mistral'])
                if 'tools' not in self.customConfig and 'parallel_tool_calls' in self.customConfig:
                    del self.customConfig['parallel_tool_calls']
            mistral_response = await self.chats(self.customConfig, self.apikey, service_name['mistral'])
            model_response = mistral_response.get("modelResponse", {})
            if not mistral_response.get('success'):
                if not self.playground:
                    await self.handle_failure(mistral_response)
                raise ValueError(mistral_response.get('error'))
            tool_calls = model_response.get('choices', [])[0].get('message', {}).get("tool_calls", [])
            if len(tool_calls) > 0 if tool_calls is not None else False:
                functionCallRes = await self.function_call(self.customConfig, service_name['mistral'], mistral_response, 0, {})
                if not functionCallRes.get('success'):
                    await self.handle_failure(functionCallRes)
                    raise ValueError(functionCallRes.get('error'))
                self.update_model_response(model_response, functionCallRes)
                tools = functionCallRes.get("tools", {})
            response = await Response_formatter(model_response, service_name['mistral'], tools, self.type, self.image_data)
            if not self.playground:
                transfer_config = functionCallRes.get('transfer_agent_config') if functionCallRes else None
                historyParams = self.prepare_history_params(response, model_response, tools, transfer_config)
        # Add transfer_agent_config to return if transfer was detected
        result = {'success': True, 'modelResponse': model_response, 'historyParams': historyParams, 'response': response}
        if functionCallRes.get('transfer_agent_config'):
            result['transfer_agent_config'] = functionCallRes['transfer_agent_config']
        return result
