from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.web_search_config import build_web_search_tool, use_web_search

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class Groq(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        conversation = ConversationService.createGroqConversation(
            self.configuration.get("conversation"), self.memory
        ).get("messages", [])
        self.customConfig["messages"] = (
            [{"role": "system", "content": self.configuration["prompt"]}]
            + conversation
            + ([{"role": "user", "content": self.user}] if self.user else [])
        )
        self.customConfig = self.service_formatter(self.customConfig, service_name["groq"])

        # Groq runs browser search server-side; the tool and which models support it come from the DB
        if use_web_search(self.service, self.model, self.built_in_tools):
            web_search_tool = build_web_search_tool(self.service)
            if web_search_tool:
                self.customConfig.setdefault("tools", []).append(web_search_tool)

        if self.stream_mode:
            groq_response = await self.stream(self.customConfig, self.apikey, service_name["groq"])
        else:
            groq_response = await self.chats(self.customConfig, self.apikey, "groq")
        model_response = groq_response.get("modelResponse", {})

        if not groq_response.get("success"):
            await self.handle_failure(groq_response)
            raise ValueError(groq_response.get("error"))

        if len(model_response.get("choices", [])[0].get("message", {}).get("tool_calls", [])) > 0:
            functionCallRes = await self.function_call(self.customConfig, service_name["groq"], groq_response, 0, {})

            if not functionCallRes.get("success"):
                await self.handle_failure(functionCallRes)
                raise ValueError(functionCallRes.get("error"))

            self.update_model_response(model_response, functionCallRes)
            tools = functionCallRes.get("tools", {})

        response = await Response_formatter(model_response, service_name["groq"], tools, self.type, self.image_data)
        transfer_config = functionCallRes.get("transfer_agent_config") if functionCallRes else None
        historyParams = self.prepare_history_params(response, model_response, tools, transfer_config)

        # Add transfer_agent_config to return if transfer was detected
        result = {
            "success": True,
            "modelResponse": model_response,
            "historyParams": historyParams,
            "response": response,
        }
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
