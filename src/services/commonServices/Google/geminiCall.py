import pydash as _
import json
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter
from google.genai import types
from urllib.parse import urlparse
import mimetypes
from src.services.commonServices.baseService.utils import serialize_config

class GeminiHandler(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == "image":
            self.customConfig["prompt"] = self.user
            gemini_response = await self.image(self.customConfig, self.apikey, service_name["gemini"])
            model_response = gemini_response.get("modelResponse", {})
            if not gemini_response.get("success"):
                if not self.playground:
                    await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get("error"))
            response = await Response_formatter(
                model_response, service_name["gemini"], tools, self.type, self.image_data
            )
            if not self.playground:
                historyParams = self.prepare_history_params(response, model_response, tools, None)
                historyParams["message"] = "image generated successfully"
                historyParams["type"] = "assistant"
        elif self.file_data or self.youtube_url:
            self.customConfig["prompt"] = self.user
            if self.youtube_url:
                self.customConfig["youtube_url"] = self.youtube_url
            gemini_response = await self.video(self.customConfig, self.apikey, service_name["gemini"])
            model_response = gemini_response.get("modelResponse", {})
            if not gemini_response.get("success"):
                if not self.playground:
                    await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get("error"))
            self.type = "video"
            response = await Response_formatter(
                model_response, service_name["gemini"], tools, self.type, self.file_data
            )
            if not self.playground:
                historyParams = self.prepare_history_params(response, model_response, tools, None)
                historyParams["type"] = "assistant"
        else:
            conversation = ConversationService.createGeminiConversation(self.configuration.get('conversation'), self.memory).get('messages', [])

            contents = conversation

            if not self.image_data and not self.audio_data:
                contents.append(types.Content(role="user", parts=[types.Part(text=self.user)]))
            else:
                user_parts = []

                if self.image_data and isinstance(self.image_data, list):
                    for image_url in self.image_data:
                        mime_type, _ = mimetypes.guess_type(urlparse(image_url).path)
                        user_parts.append(types.Part.from_uri(file_uri=image_url, mime_type=mime_type))

                if self.audio_data and isinstance(self.audio_data, list):
                    for audio_url in self.audio_data:
                        mime_type, _ = mimetypes.guess_type(urlparse(audio_url).path)
                        user_parts.append(types.Part.from_uri(file_uri=audio_url, mime_type=mime_type))
                
                user_parts.append(types.Part(text=self.user))

                if user_parts:
                    contents.append(types.Content(role='user', parts=user_parts))

            if self.configuration.get('prompt'):
                self.customConfig['system_instruction'] = self.configuration['prompt']

            self.customConfig = self.service_formatter(self.customConfig, service_name['gemini'])
            self.customConfig["contents"] = contents
        
            gemini_response = await self.chats(self.customConfig, self.apikey, service_name['gemini'])
            model_response = gemini_response.get('modelResponse', {})
            if not gemini_response.get('success'):
                if not self.playground:
                    await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get('error'))
            

            candidates = model_response.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                has_function_calls = any(isinstance(part, dict) and part.get('function_call') is not None for part in parts)
                if has_function_calls:
                    functionCallRes = await self.function_call(self.customConfig, service_name['gemini'], gemini_response)
                    if not functionCallRes.get('success'):
                        await self.handle_failure(functionCallRes)
                        raise ValueError(functionCallRes.get('error'))

                    tools = functionCallRes.get('tools', {})
                    self.update_model_response(model_response, functionCallRes)
                    model_response = functionCallRes.get('modelResponse', model_response)
                    response = await Response_formatter(functionCallRes.get('modelResponse', {}), service_name['gemini'], tools, self.type, self.image_data)
                else:
                    response = await Response_formatter(model_response, service_name['gemini'], {}, self.type, self.image_data)

                if not self.playground:
                    transfer_config = functionCallRes.get('transfer_agent_config') if functionCallRes else None
                    self.customConfig = serialize_config(self.customConfig) 
                    historyParams = self.prepare_history_params(response, model_response, tools, transfer_config)
        
        result = {'success': True, 'modelResponse': model_response, 'historyParams': historyParams, 'response': response}
        if functionCallRes.get('transfer_agent_config'):
            result['transfer_agent_config'] = functionCallRes['transfer_agent_config']
        return result
