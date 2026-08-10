import base64
import json

from globals import logger
from src.configs.constant import service_name
from src.configs.model_configuration import model_config_document
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.gcp_upload_service import uploadDoc

from ..baseService.baseService import BaseService
from src.services.utils.mcp_utils import merge_server_side_mcp_into_tools
from ..createConversations import ConversationService


class OpenaiResponse(BaseService):
    _PREV_RESP_KEY_PREFIX = "openai_prev_resp"
    _PREV_RESP_TTL = 2592000  # 30 days, mirrors OpenAI response retention

    def _previous_response_key(self):
        return (
            f"{self._PREV_RESP_KEY_PREFIX}_"
            f"{self.org_id}_{self.bridge_id}_"
            f"{self.thread_id}_{self.sub_thread_id}"
        )

    async def _get_previous_response_id(self):
        try:
            cached = await find_in_cache(self._previous_response_key())
            if cached is None:
                return None
            # Cache values are JSON-encoded, so a plain string id is stored as
            # '"resp_..."'. Decode it to return the bare id.
            if isinstance(cached, str):
                try:
                    return json.loads(cached)
                except json.JSONDecodeError:
                    return cached
            return cached
        except Exception as error:
            logger.error(f"Error fetching previous_response_id: {error}")
            return None

    async def _set_previous_response_id(self, response_id):
        if not response_id:
            return
        try:
            await store_in_cache(
                self._previous_response_key(), response_id, ttl=self._PREV_RESP_TTL
            )
        except Exception as error:
            logger.error(f"Failed to cache OpenAI previous_response_id: {error}")

    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == "image":
            self.customConfig["prompt"] = self.user
            openAIResponse = await self.image(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})
            if not openAIResponse.get("success"):
                await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))
            response = await Response_formatter(
                modelResponse, service_name["openai"], tools, self.type, self.image_data
            )
            historyParams = self.prepare_history_params(response, modelResponse, tools, None)
            historyParams["message"] = "image generated successfully"
            historyParams["type"] = "assistant"
        else:
            previous_response_id = await self._get_previous_response_id()
            if previous_response_id:
                self.customConfig["previous_response_id"] = previous_response_id

            # When chaining via previous_response_id, the prior context is stored
            # on OpenAI's side, so we only need to send the current turn. We also
            # skip our custom memory injection to test OpenAI's native context
            # management. Fallback to a different service still uses the full
            # DB-built conversation and memory.
            conversation = ConversationService.createOpenAiConversation(
                None if previous_response_id else self.configuration.get("conversation"),
                None if previous_response_id else self.memory,
                self.files,
                include_history=not bool(previous_response_id),
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
                if "tools" in model_config_document[self.service][self.model]["configuration"]:
                    if "tools" not in self.customConfig:
                        self.customConfig["tools"] = []

                    tools_to_append = []

                    if "web_search" in self.built_in_tools:
                        if self.web_search_filters and isinstance(self.web_search_filters, list):
                            web_search_tool = {
                                "type": "web_search",
                                "filters": {"allowed_domains": self.web_search_filters},
                            }
                        else:
                            web_search_tool = {"type": "web_search_preview"}
                        tools_to_append.append(web_search_tool)

                    if "image_generation" in self.built_in_tools:
                        image_generation_tool = {"type": "image_generation"}
                        tools_to_append.append(image_generation_tool)

                    self.customConfig["tools"].extend(tools_to_append)

            if self.stream_mode:
                openAIResponse = await self.stream(self.customConfig, self.apikey, service_name["openai"])
            else:
                openAIResponse = await self.chats(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})

            for item in modelResponse.get("output", []):
                if item.get("type") == "image_generation_call" and item.get("result"):
                    image_bytes = base64.b64decode(item["result"].strip())
                    gcp_url = await uploadDoc(
                        file=image_bytes,
                        folder="generated-images",
                        real_time=True,
                        content_type="image/png",
                    )
                    item["image_url"] = gcp_url
                    item["permanent_url"] = gcp_url
                    item.pop("result", None)

            if not openAIResponse.get("success"):
                await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))

            # Check for function calls — streaming returns has_tool_calls flag directly
            if self.stream_mode:
                has_function_call = openAIResponse.get("has_tool_calls", False)
            else:
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
                final_model_response = functionCallRes.get("modelResponse", {})
                tools = merge_server_side_mcp_into_tools(
                    service_name["openai"], final_model_response, functionCallRes.get("tools", {})
                )
                response = await Response_formatter(
                    final_model_response,
                    service_name["openai"],
                    tools,
                    self.type,
                    self.image_data,
                )
            else:
                tools = merge_server_side_mcp_into_tools(
                    service_name["openai"], modelResponse, {}
                )
                response = await Response_formatter(
                    modelResponse, service_name["openai"], tools, self.type, self.image_data
                )

            transfer_config = (
                functionCallRes.get("transfer_agent_config") if has_function_call and functionCallRes else None
            )
            historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)

            # Persist the OpenAI response id so the next turn can chain via
            # previous_response_id instead of resending the full conversation.
            # After tool calls, use the final recursive response, not the
            # initial function-call response.
            final_response_id = (
                functionCallRes.get("modelResponse", {}).get("id")
                if has_function_call and functionCallRes
                else modelResponse.get("id")
            )
            if final_response_id:
                await self._set_previous_response_id(final_response_id)

        # Add transfer_agent_config to return if transfer was detected
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
