import pydash as _
import json
from ..baseService.baseService import BaseService
from ..createConversations import ConversationService
from globals import logger
from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.formatters.web_search_extractor import gemini_web_search_count
from src.services.utils.web_search_config import build_web_search_tool, use_web_search
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
                await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get("error"))
            response = await Response_formatter(
                model_response, service_name["gemini"], tools, self.type, self.image_data
            )
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
                await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get("error"))
            self.type = "video"
            response = await Response_formatter(
                model_response, service_name["gemini"], tools, self.type, self.file_data
            )
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

            # Google Search grounding; the tool ({"google_search": {}}) and per-model support come from the DB
            web_search_tool = (
                build_web_search_tool(self.service)
                if use_web_search(self.service, self.model, self.built_in_tools)
                else None
            )
            if web_search_tool:
                search_tool = types.Tool(**web_search_tool)
                generate_config = self.customConfig["config"]
                existing_tools = generate_config.tools or []
                function_tool = next((tool for tool in existing_tools if tool.function_declarations), None)
                if function_tool is None:
                    generate_config.tools = existing_tools + [search_tool]
                elif self.model.startswith("gemini-3") and not self.stream_mode:
                    # Gemini 3 can combine built-in and function tools only with server-side tool invocations on.
                    # Not in stream mode: the stream runner drops the tool_call/tool_response parts and thought
                    # signatures that must be sent back unchanged on the next turn.
                    for field in search_tool.model_dump(exclude_none=True):
                        setattr(function_tool, field, getattr(search_tool, field))
                    tool_config = generate_config.tool_config or types.ToolConfig()
                    tool_config.include_server_side_tool_invocations = True
                    generate_config.tool_config = tool_config
                else:
                    logger.warning(
                        f"Skipping Gemini web search for {self.model}: google_search cannot be combined with "
                        f"function calling on this model or in stream mode"
                    )
        
            if self.stream_mode:
                gemini_response = await self.stream(self.customConfig, self.apikey, service_name['gemini'])
            else:
                gemini_response = await self.chats(self.customConfig, self.apikey, service_name['gemini'])
            model_response = gemini_response.get('modelResponse', {})
            if not gemini_response.get('success'):
                await self.handle_failure(gemini_response)
                raise ValueError(gemini_response.get('error'))
            

            # Count before the function-call loop overwrites the first turn's content
            web_search_count = gemini_web_search_count(model_response)

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
                    web_search_count += gemini_web_search_count(functionCallRes.get('modelResponse', {}))
                    self.update_model_response(model_response, functionCallRes)
                    model_response = functionCallRes.get('modelResponse', model_response)
                    response = await Response_formatter(functionCallRes.get('modelResponse', {}), service_name['gemini'], tools, self.type, self.image_data)
                else:
                    response = await Response_formatter(model_response, service_name['gemini'], {}, self.type, self.image_data)

                if web_search_count and self.token_calculator:
                    self.token_calculator.add_web_search_calls(web_search_count)

                transfer_config = functionCallRes.get('transfer_agent_config') if functionCallRes else None
                self.customConfig = serialize_config(self.customConfig)
                historyParams = self.prepare_history_params(response, model_response, tools, transfer_config)
        
        result = {'success': True, 'modelResponse': model_response, 'historyParams': historyParams, 'response': response}
        if functionCallRes.get('transfer_agent_config'):
            result['transfer_agent_config'] = functionCallRes['transfer_agent_config']
        return result
