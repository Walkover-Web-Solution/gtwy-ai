from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class Grok(BaseService):
    async def execute(self):
        history_params = {}
        tools = {}
        function_call_response = {}

        conversation = ConversationService.createGrokConversation(
            self.configuration.get("conversation"), self.memory, self.files, self.image_data
        ).get("messages", [])

        messages = [{"role": "system", "content": self.configuration["prompt"]}] + conversation

        if self.image_data:
            user_content = []
            if self.user:
                user_content.append({"type": "text", "text": self.user})
            if isinstance(self.image_data, list):
                for image_url in self.image_data:
                    user_content.append({"type": "image_url", "image_url": {"url": image_url}})
            if user_content:
                messages.append({"role": "user", "content": user_content})
        elif self.user:
            messages.append({"role": "user", "content": self.user})

        self.customConfig["messages"] = messages
        self.customConfig = self.service_formatter(self.customConfig, service_name["grok"])

        grok_response = await self.chats(self.customConfig, self.apikey, service_name["grok"])
        model_response = grok_response.get("modelResponse", {})

        if not grok_response.get("success"):
            if not self.playground:
                await self.handle_failure(grok_response)
            raise ValueError(grok_response.get("error"))

        choices = model_response.get("choices") or []
        first_choice = choices[0] if choices else {}
        tool_calls = first_choice.get("message", {}).get("tool_calls", []) or []

        if tool_calls:
            function_call_response = await self.function_call(
                self.customConfig, service_name["grok"], grok_response, 0, {}
            )

            if not function_call_response.get("success"):
                if not self.playground:
                    await self.handle_failure(function_call_response)
                raise ValueError(function_call_response.get("error"))

            self.update_model_response(model_response, function_call_response)
            tools = function_call_response.get("tools", {})

        response = await Response_formatter(model_response, service_name["grok"], tools, self.type, self.image_data)

        if not self.playground:
            transfer_config = function_call_response.get("transfer_agent_config") if function_call_response else None
            history_params = self.prepare_history_params(response, model_response, tools, transfer_config)

        result = {
            "success": True,
            "modelResponse": model_response,
            "historyParams": history_params,
            "response": response,
        }
        if function_call_response.get("transfer_agent_config"):
            result["transfer_agent_config"] = function_call_response["transfer_agent_config"]
        return result
