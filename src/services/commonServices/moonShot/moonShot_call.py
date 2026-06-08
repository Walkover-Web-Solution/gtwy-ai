from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.apiservice import fetch_images_b64

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class MoonShot(BaseService):
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
                    [{"role": "system", "content": self.configuration["prompt"]}]
                    + conversation
                    + ([{"role": "user", "content": self.user}] if self.user else [])
                )
            else:
                self.customConfig["messages"] = [
                    {"role": "system", "content": self.configuration["prompt"]}
                ] + conversation
                if self.user:
                    user_content = [{"type": "text", "text": self.user}]
                    if isinstance(self.image_data, list):
                        # Moonshot requires base64 encoded images, not URLs
                        # Use existing fetch_images_b64 function to download and convert images
                        images_with_mime = await fetch_images_b64(self.image_data)
                        for base64_image, mime_type in images_with_mime:
                            base64_url = f"data:{mime_type};base64,{base64_image}"
                            user_content.append({"type": "image_url", "image_url": {"url": base64_url}})
                    self.customConfig["messages"].append({"role": "user", "content": user_content})
            self.customConfig = self.service_formatter(self.customConfig, service_name["moonshot"])
            if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
                del self.customConfig["parallel_tool_calls"]
        if self.stream_mode:
            moonShotResponse = await self.stream(self.customConfig, self.apikey, service_name["moonshot"])
        else:
            moonShotResponse = await self.chats(self.customConfig, self.apikey, service_name["moonshot"])
        modelResponse = moonShotResponse.get("modelResponse", {})
        if not moonShotResponse.get("success"):
            await self.handle_failure(moonShotResponse)
            raise ValueError(moonShotResponse.get("error"))
        if len(modelResponse.get("choices", [])[0].get("message", {}).get("tool_calls", [])) > 0:
            functionCallRes = await self.function_call(
                self.customConfig, service_name["moonshot"], moonShotResponse, 0, {}
            )
            if not functionCallRes.get("success"):
                await self.handle_failure(functionCallRes)
                raise ValueError(functionCallRes.get("error"))
            self.update_model_response(modelResponse, functionCallRes)
            tools = functionCallRes.get("tools", {})
        response = await Response_formatter(
            modelResponse, service_name["moonshot"], tools, self.type, self.image_data
        )
        transfer_config = functionCallRes.get("transfer_agent_config") if functionCallRes else None
        historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
