from src.configs.service_registry import prompt_role
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.apiservice import fetch_images_b64

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService


class OpenAICompatibleHandler(BaseService):
    """Single handler for OpenAI-Chat-Completions-compatible services that use the
    generic AsyncOpenAI runner (open_router, neev_cloud, moonshot,
    openai_completion, + future ones).

    Behavior-identical to the former per-service call classes; the only
    per-service variation is the system-prompt role (``prompt_role`` from the
    registry: "system" by default, "developer" for open_router and
    openai_completion).
    """

    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        service = self.service
        role = prompt_role(service)
        conversation = ConversationService.createOpenAICompatibleConversation(
            self.configuration.get("conversation"), self.memory
        ).get("messages", [])
        if self.reasoning_model:
            self.customConfig["messages"] = conversation + (
                [{"role": "user", "content": self.user}] if self.user else []
            )
        else:
            if not self.image_data:
                self.customConfig["messages"] = (
                    [{"role": role, "content": self.configuration["prompt"]}]
                    + conversation
                    + ([{"role": "user", "content": self.user}] if self.user else [])
                )
            else:
                self.customConfig["messages"] = [
                    {"role": role, "content": self.configuration["prompt"]}
                ] + conversation
                # Send the user message whenever there is text or images, so an
                # image-only turn still reaches the model.
                user_content = []
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
            self.customConfig = self.service_formatter(self.customConfig, service)
            if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
                del self.customConfig["parallel_tool_calls"]
        if self.stream_mode:
            providerResponse = await self.stream(self.customConfig, self.apikey, service)
        else:
            providerResponse = await self.chats(self.customConfig, self.apikey, service)
        modelResponse = providerResponse.get("modelResponse", {})
        if not providerResponse.get("success"):
            await self.handle_failure(providerResponse)
            raise ValueError(providerResponse.get("error"))
        if len(modelResponse.get("choices", [])[0].get("message", {}).get("tool_calls", [])) > 0:
            functionCallRes = await self.function_call(
                self.customConfig, service, providerResponse, 0, {}
            )
            if not functionCallRes.get("success"):
                await self.handle_failure(functionCallRes)
                raise ValueError(functionCallRes.get("error"))
            self.update_model_response(modelResponse, functionCallRes)
            tools = functionCallRes.get("tools", {})
        response = await Response_formatter(
            modelResponse, service, tools, self.type, self.image_data
        )
        transfer_config = functionCallRes.get("transfer_agent_config") if functionCallRes else None
        historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
