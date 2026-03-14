from src.configs.constant import service_name
from src.configs.model_configuration import model_config_document
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class OpenaiResponse(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == "image":
            self.customConfig["prompt"] = self.user
            openAIResponse = await self.image(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})
            if not openAIResponse.get("success"):
                if not self.playground:
                    await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))
            response = await Response_formatter(
                modelResponse, service_name["openai"], tools, self.type, self.image_data
            )
            if not self.playground:
                historyParams = self.prepare_history_params(response, modelResponse, tools, None)
                historyParams["message"] = "image generated successfully"
                historyParams["type"] = "assistant"
        else:
            conversation = ConversationService.createOpenAiConversation(
                self.configuration.get("conversation"), self.memory, self.files
            ).get("messages", [])
            developer = (
                [{"role": "developer", "content": self.configuration["prompt"]}] if not self.reasoning_model else []
            )

            if self.image_data and isinstance(self.image_data, list):
                self.customConfig["input"] = developer + conversation
                image_content = [{"type": "input_image", "image_url": url} for url in self.image_data]
                content = [{"type": "input_text", "text": self.user}] + image_content if self.user else image_content
                self.customConfig["input"].append({"role": "user", "content": content})
            elif self.files and len(self.files) > 0:
                self.customConfig["input"] = developer + conversation
                file_content = [{"type": "input_file", "file_url": file_url} for file_url in self.files]
                content = [{"type": "input_text", "text": self.user}] + file_content if self.user else file_content
                self.customConfig["input"].append({"role": "user", "content": content})
            else:
                user = [{"role": "user", "content": self.user}] if self.user else []
                self.customConfig["input"] = developer + conversation + user

            self.customConfig = self.service_formatter(self.customConfig, service_name["openai"])

            if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
                del self.customConfig["parallel_tool_calls"]

            if len(self.built_in_tools) > 0:
                if (
                    "web_search" in self.built_in_tools
                    and "tools" in model_config_document[self.service][self.model]["configuration"]
                ):
                    if "tools" not in self.customConfig:
                        self.customConfig["tools"] = []

                    if self.web_search_filters and isinstance(self.web_search_filters, list):
                        web_search_tool = {
                            "type": "web_search",
                            "filters": {"allowed_domains": self.web_search_filters},
                        }
                    else:
                        web_search_tool = {"type": "web_search_preview"}

                    self.customConfig["tools"].append(web_search_tool)

            openAIResponse = await self.chats(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})

            if not openAIResponse.get("success"):
                if not self.playground:
                    await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))

            # Check for function calls in multiple possible locations with fallback
            has_function_call = (
                any(output.get("type") == "function_call" for output in modelResponse.get("output", []))
                or any(output.get("type") == "tool_call" for output in modelResponse.get("output", []))
                or any(
                    "function_call" in str(output)
                    for output in modelResponse.get("output", [])
                    if output.get("type") in ["reasoning", "message", "output_text"]
                )
            )

            if has_function_call:
                functionCallRes = await self.function_call(
                    self.customConfig, service_name["openai"], openAIResponse, 0, {}
                )
                if not functionCallRes.get("success"):
                    await self.handle_failure(functionCallRes)
                    raise ValueError(functionCallRes.get("error"))
                self.update_model_response(modelResponse, functionCallRes)
                response = await Response_formatter(
                    functionCallRes.get("modelResponse", {}),
                    service_name["openai"],
                    functionCallRes.get("tools", {}),
                    self.type,
                    self.image_data,
                )
                tools = functionCallRes.get("tools", {})
            else:
                response = await Response_formatter(
                    modelResponse, service_name["openai"], {}, self.type, self.image_data
                )

            if not self.playground:
                transfer_config = (
                    functionCallRes.get("transfer_agent_config") if has_function_call and functionCallRes else None
                )
                historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)

        # Add transfer_agent_config to return if transfer was detected
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
