from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService
from .deepgramModelRun import deepgram_runmodel


class Deepgram(BaseService):
    async def execute(self):
        history_params = {}

        # audio_url may come from user_urls (audio type) or directly as the user message for URL-only requests
        audio_url = next(iter(self.audio_data or []), None) or (
            self.user if isinstance(self.user, str) and self.user.lower().startswith(("http://", "https://")) else None
        )

        if not audio_url:
            raise ValueError("For deepgram service, provide at least one audio URL in user_urls with type 'audio'.")

        self.customConfig["audio_url"] = audio_url
        self.customConfig = self.service_formatter(self.customConfig, service_name["deepgram"])

        deepgram_response = await self.chats(self.customConfig, self.apikey, service_name["deepgram"])
        model_response = deepgram_response.get("modelResponse", {})

        if not deepgram_response.get("success"):
            if not self.playground:
                await self.handle_failure(deepgram_response)
            raise ValueError(deepgram_response.get("error"))

        response = await Response_formatter(model_response, service_name["deepgram"], {}, self.type, self.image_data)
        if not self.playground:
            history_params = self.prepare_history_params(response, model_response, {})

        return {
            "success": True,
            "modelResponse": model_response,
            "historyParams": history_params,
            "response": response,
        }
