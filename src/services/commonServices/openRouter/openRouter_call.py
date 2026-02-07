from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class OpenRouter(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        conversation = ConversationService.createOpenRouterConversation(
            self.configuration.get("conversation"), self.memory
        ).get("messages", [])
        if self.reasoning_model:
            self.customConfig["messages"] = conversation + (
                [{"role": "user", "content": self.user}] if self.user else []
            )
        else:
            if not self.image_data:
                self.customConfig["messages"] = (
                    [{"role": "developer", "content": self.configuration["prompt"]}]
                    + conversation
                    + ([{"role": "user", "content": self.user}] if self.user else [])
                )
            else:
                self.customConfig["messages"] = [
                    {"role": "developer", "content": self.configuration["prompt"]}
                ] + conversation
                if self.user:
                    user_content = [{"type": "text", "text": self.user}]
                    if isinstance(self.image_data, list):
                        for image_url in self.image_data:
                            user_content.append({"type": "image_url", "image_url": {"url": image_url}})
                    self.customConfig["messages"].append({"role": "user", "content": user_content})
            self.customConfig = self.service_formatter(self.customConfig, service_name["open_router"])
            if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
                del self.customConfig["parallel_tool_calls"]
        openRouterResponse = await self.chats(self.customConfig, self.apikey, service_name["open_router"])
        modelResponse = openRouterResponse.get("modelResponse", {})
        if not openRouterResponse.get("success"):
            if not self.playground:
                await self.handle_failure(openRouterResponse)
            raise ValueError(openRouterResponse.get("error"))
        if len(modelResponse.get("choices", [])[0].get("message", {}).get("tool_calls", [])) > 0:
            functionCallRes = await self.function_call(
                self.customConfig, service_name["open_router"], openRouterResponse, 0, {}
            )
            if not functionCallRes.get("success"):
                await self.handle_failure(functionCallRes)
                raise ValueError(functionCallRes.get("error"))
            self.update_model_response(modelResponse, functionCallRes)
            tools = functionCallRes.get("tools", {})
        response = await Response_formatter(
            modelResponse, service_name["open_router"], tools, self.type, self.image_data
        )
        if not self.playground:
            transfer_config = functionCallRes.get("transfer_agent_config") if functionCallRes else None
            historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)
        # Add transfer_agent_config to return if transfer was detected
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
