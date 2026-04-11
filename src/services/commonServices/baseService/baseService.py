import asyncio
import json
import traceback
from concurrent.futures import ThreadPoolExecutor

import pydash as _

from config import Config
from globals import logger
from src.configs.serviceKeys import ServiceKeys

from ....configs.constant import service_name
from ....db_services import metrics_service
from ....services.cache_service import make_json_serializable
from ....services.commonServices.queueService.queueLogService import sub_queue_obj
from ..anthropic.anthropicModelRun import anthropic_runmodel, anthropic_stream
from ..deepgram.deepgramModelRun import deepgram_runmodel
from ..Google.gemini_image_model import gemini_image_model
from ..Google.gemini_modelrun import gemini_modelrun, gemini_modelrun_stream
from ..Google.gemini_video_model import gemini_video_model
from ..grok.grokModelRun import grok_runmodel, grok_stream
from ..groq.groqModelRun import groq_runmodel, groq_stream
from ..Mistral.mistral_model_run import mistral_model_run, mistral_stream
from ..openAI.image_model import OpenAIImageModel
from ..openAI.runModel import openai_completion, openai_response_model, openai_response_stream
from ..openAI.openai_stream_utils import sanitize_openai_response_item
from ..openRouter.openRouter_modelrun import openrouter_modelrun, openrouter_stream
from ..streaming_service import StreamingService
from .utils import (
    build_accumulated_response,
    make_code_mapping_by_service,
    process_data_and_run_tools,
    run_stream_and_collect,
    sendResponse,
    tool_call_formatter,
    validate_tool_call,
    reasoning_formatter
)
from src.exceptions import ApiCallError

executor = ThreadPoolExecutor(max_workers=int(Config.max_workers) or 10)


class BaseService:
    def __init__(self, params):
        self.customConfig = params.get("customConfig")
        self.configuration = params.get("configuration")
        self.apikey = params.get("apikey")
        self.variables = params.get("variables")
        self.user = params.get("user")
        self.original_user = params.get("original_user")
        self.tool_call = params.get("tools")
        self.org_id = params.get("org_id")
        self.bridge_id = params.get("bridge_id")
        self.bridge = params.get("bridge")
        self.thread_id = params.get("thread_id")
        self.sub_thread_id = params.get("sub_thread_id")
        self.model = params.get("model")
        self.service = params.get("service")
        self.modelOutputConfig = params.get("modelOutputConfig")
        self.playground = params.get("playground")
        self.template = params.get("template")
        self.response_format = params.get("response_format")
        self.execution_time_logs = params.get("execution_time_logs", {})
        self.timer = params.get("timer")
        self.func_tool_call_data = []
        self.variables_path = params.get("variables_path")
        self.message_id = params.get("message_id")
        self.bridgeType = params.get("bridgeType")
        self.reasoning_model = params.get("reasoning_model")
        self.memory = params.get("memory")
        self.type = params.get("type")
        self.token_calculator = params.get("token_calculator")
        self.apikey_object_id = params.get("apikey_object_id")
        self.apikey_status = params.get("apikey_status")
        self.image_data = params.get("images")
        self.audio_data = params.get("audios")
        self.maximum_iterations = params.get("maximum_iterations")
        self.text = params.get("text")
        self.tool_id_and_name_mapping = params.get("tool_id_and_name_mapping")
        self.batch = params.get("batch")
        self.webhook = params.get("webhook")
        self.batch_variables = params.get("batch_variables")
        self.processed_prompts = params.get("processed_prompts")
        self.name = params.get("name")
        self.org_name = params.get("org_name")
        self.built_in_tools = params.get("built_in_tools")
        self.function_time_logs = params.get("function_time_logs")
        self.files = params.get("files") or []
        self.file_data = params.get("file_data")
        self.youtube_url = params.get("youtube_url")
        self.web_search_filters = params.get("web_search_filters")
        self.folder_id = params.get("folder_id")
        self.bridge_configurations = params.get("bridge_configurations")
        self.owner_id = params.get("owner_id")
        self.is_embed = params.get("is_embed")
        self.user_id = params.get("user_id")
        self.api_collection = params.get("api_collection")
        self.tool_call_limit_error = None
        self.stream_mode = params.get("customConfig", {}).get("stream") is True
        if self.stream_mode:
            self.streamer = StreamingService(mode="sse")
        else:
            self.streamer = None

        self.stream_mode = params.get("customConfig", {}).get("stream") is True
        if self.stream_mode:
            self.streamer = StreamingService(mode="sse")
        else:
            self.streamer = None

    def aiconfig(self):
        return self.customConfig

    async def run_tool(self, responses, service):
        codes_mapping, function_list = make_code_mapping_by_service(responses, service)
        if not self.playground:
            asyncio.create_task(
                sendResponse(self.response_format, data={"function_call": True, "Name": function_list}, success=True)
            )
        codes_mapping = await self.replace_variables_in_args(codes_mapping)

        # Check for transfer action in codes_mapping
        from src.services.utils.transfer_handler import check_transfer_from_codes_mapping

        has_transfer, transfer_config = check_transfer_from_codes_mapping(codes_mapping, self.tool_id_and_name_mapping)

        if has_transfer:
            # Return transfer config instead of processing tools
            return [], [], {"transfer_agent_config": transfer_config}

        return await process_data_and_run_tools(codes_mapping, self)

    def update_configration(self, response, function_responses, configuration, mapping_response_data, service, tools):
        if service == "anthropic":
            configuration["messages"].append({"role": "assistant", "content": response["content"]})
            configuration["messages"].append({"role": "user", "content": []})

        for index, function_response in enumerate(function_responses):
            tools[function_response["name"]] = function_response["content"]

            match service:
                case 'openai_completion' | 'groq' | 'grok' | 'open_router' | 'mistral':
                    assistant_tool_calls = response['choices'][0]['message']['tool_calls'][index]
                    configuration['messages'].append({'role': 'assistant', 'content': None, 'tool_calls': [assistant_tool_calls]})
                    tool_calls_id = assistant_tool_calls['id']
                    configuration['messages'].append(mapping_response_data[tool_calls_id])
                case 'openai':
                    should_sanitize_openai_items = bool(self.stream_mode)

                    # First, add all reasoning outputs to the configuration
                    for output in response["output"]:
                        if output.get("type") == "reasoning":
                            configuration["input"].append(
                                sanitize_openai_response_item(output)
                                if should_sanitize_openai_items
                                else output
                            )

                    # Then handle function calls using the index parameter
                    function_call_outputs = [
                        output for output in response["output"] if output.get("type") == "function_call"
                    ]
                    if index < len(function_call_outputs):
                        output = (
                            sanitize_openai_response_item(function_call_outputs[index])
                            if should_sanitize_openai_items
                            else function_call_outputs[index]
                        )
                        configuration['input'].append(output)
                        tool_calls_id = output['id']
                        configuration['input'].append({                           
                            "type": "function_call_output",
                            "call_id": output['call_id'],
                            "output": mapping_response_data[tool_calls_id]['content']
                        })
                case 'anthropic':
                    ordered_json = {"type":"tool_result",  
                                                 "tool_use_id": function_response['tool_call_id'],
                                                 "content": function_response['content']}
                    configuration['messages'][-1]['content'].append(ordered_json)
                case 'gemini':
                    from google.genai import types

                    if index == 0:
                        configuration['contents'].append(response.get('candidates', [{}])[0].get('content', {}))

                    function_response_content = function_response['content']
                    if isinstance(function_response_content, str):
                        try:
                            function_response_content = json.loads(function_response_content)
                        except:
                            pass

                    function_response_content = {"result": function_response_content}
                    function_response_part = types.Part.from_function_response(
                        name=function_response['name'],
                        response=function_response_content
                    )
                    configuration['contents'].append(types.Content(role='user', parts=[function_response_part]))
                case  _:
                    pass
        return configuration, tools

    async def function_call(self, configuration, service, response, loop_count=0, tools=None):
        if tools is None:
            tools = {}
        if not response.get("success"):
            return {"success": False, "error": response.get("error")}

        model_response = response.get("modelResponse", {})
        if configuration.get("tool_choice") is not None and configuration["tool_choice"] not in ["auto", "none"]:
            if service == "anthropic":
                configuration["tool_choice"] = {"type": "auto"}
            else:
                configuration["tool_choice"] = "auto"
        if validate_tool_call(service, model_response) and loop_count <= int(self.maximum_iterations or 0):
            loop_count += 1
        else:
            if validate_tool_call(service, model_response):
                tool_call_limit_msg = "Execution stopped in between because tool call limit exceeded."
                response["error"] = tool_call_limit_msg
                self.tool_call_limit_error = tool_call_limit_msg
            if self.stream_mode and self.streamer and response.get("has_tool_calls"):
                response["stream_finish_reason"] = "tool_call_limit_reached"
            return response
        func_response_data, mapping_response_data, tools_call_data = await self.run_tool(model_response, service)
        self.func_tool_call_data.append(tools_call_data)

        # Check if transfer was detected in run_tool
        if isinstance(tools_call_data, dict) and "transfer_agent_config" in tools_call_data:
            if self.stream_mode and self.streamer:
                response["stream_finish_reason"] = "tool_transfer"
            response["transfer_agent_config"] = tools_call_data["transfer_agent_config"]
            return response

        if self.stream_mode and self.streamer:
            for tool_result in func_response_data:
                await self.streamer.emit_tool_result(
                    name=tool_result.get("name", ""),
                    content=tool_result.get("content", ""),
                    call_id=tool_result.get("tool_call_id", ""),
                )

        configuration, tools = self.update_configration(
            model_response, func_response_data, configuration, mapping_response_data, service, tools
        )
        if not self.playground and not self.stream_mode:
            asyncio.create_task(
                sendResponse(
                    self.response_format,
                    data={"function_call": True, "success": True, "message": "Continuing AI reasoning…"},
                    success=True,
                )
            )
        if self.stream_mode and self.streamer:
            ai_response = await self.stream(configuration, self.apikey, service, loop_count)
        else:
            ai_response = await self.chats(configuration, self.apikey, service, loop_count)
        ai_response["tools"] = tools
        return await self.function_call(configuration, service, ai_response, loop_count, tools)

    async def handle_failure(self, response):
        latency = {
            "over_all_time": self.timer.stop("Api total time") or "",
            "model_execution_time": sum(self.execution_time_logs.values()) or "",
            "execution_time_logs": self.execution_time_logs or {},
        }
        usage = {
            "service": self.service,
            "model": self.model,
            "orgId": self.org_id,
            "latency": json.dumps(latency),
            "success": False,
            "error": response.get("error"),
            "apikey_object_id": self.apikey_object_id,
        }
        history_params = {
            "thread_id": self.thread_id,
            "sub_thread_id": self.sub_thread_id,
            "user": self.original_user or json.dumps(self.tool_call),
            "message": "",
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "model": self.configuration.get("model"),
            "channel": "chat",
            "type": "error",
            "actor": "user" if self.user else "tool",
            "message_id": self.message_id,
        }
        payload = metrics_service.build_history_and_metrics_payload([usage], history_params, None)
        await asyncio.gather(
            sub_queue_obj.publish_message(make_json_serializable({"save_history": [payload]})),
            sendResponse(self.response_format, data=response.get("error")),
            return_exceptions=True,
        )

    # todo
    def update_model_response(self, model_response, functionCallRes=None):
        if functionCallRes is None:
            functionCallRes = {}
        funcModelResponse = functionCallRes.get("modelResponse", {})
        if self.service in [
            service_name["openai"],
            service_name["groq"],
            service_name["grok"],
            service_name["anthropic"],
            service_name["open_router"],
            service_name["mistral"],
            service_name["gemini"],
            service_name["openai_completion"],
        ]:
            if funcModelResponse and self.service != service_name["openai"]:
                _.set_(
                    model_response,
                    self.modelOutputConfig["message"],
                    _.get(funcModelResponse, self.modelOutputConfig["message"]),
                )
                if self.service in [
                    service_name["openai_completion"],
                    service_name["groq"],
                    service_name["grok"],
                    service_name["open_router"],
                    service_name["gemini"],
                ]:
                    _.set_(
                        model_response,
                        self.modelOutputConfig["tools"],
                        _.get(funcModelResponse, self.modelOutputConfig["tools"]),
                    )

    def prepare_history_params(self, response, model_response, tools, transfer_agent_config=None, is_cached=False):
        # Get the original message content
        original_message = response.get("data", {}).get("content") or ""
        reasoning = response.get("data", {}).get("reasoning")

        # If message is empty but we have transfer config, create custom message
        if not original_message and transfer_agent_config:
            agent_name = transfer_agent_config.get("tool_name", "the agent")
            original_message = f"Query is successfully transferred to agent {agent_name}"
        return {
            "thread_id": self.thread_id,
            "sub_thread_id": self.sub_thread_id,
            "user": self.original_user or "",
            "message": original_message,
            "reasoning": reasoning,
            "org_id": self.org_id,
            "bridge_id": self.bridge_id,
            "model": model_response.get("model") or self.configuration.get("model"),
            "service": self.service,
            "channel": "chat",
            "type": "assistant",
            "actor": "user",
            "tools": tools,
            "chatbot_message": "",
            "tools_call_data": self.func_tool_call_data,
            "message_id": self.message_id,
            "llm_urls": [
                {"revised_prompt": img.get("revised_prompt"), "permanent_url": img.get("url"), "type": "image"}
                for img in model_response.get("data", [])
                if img.get("url")
            ]
            or [
                {
                    "revised_prompt": model_response.get("data", [{}])[0].get("revised_prompt", None),
                    "permanent_url": model_response.get("data", [{}])[0].get("url", None),
                }
            ]
            if model_response.get("data", [{}])[0].get("url")
            else [],
            "revised_prompt": model_response.get("data", [{}])[0].get("revised_prompt", None),
            "user_urls": [
                *({"url": u, "type": "image"} for u in (self.image_data or [])),
                *({"url": u, "type": "pdf"} for u in (self.files or [])),
                *({"url": u, "type": "audio"} for u in (self.audio_data or [])),
            ],
            "AiConfig": self.customConfig,
            "firstAttemptError": model_response.get("firstAttemptError") or "",
            "annotations": _.get(model_response, self.modelOutputConfig.get("annotations")) or [],
            "fallback_model": (
                self.bridge_configurations.get(self.bridge_id, {}).get("fall_back")
                if self.bridge_configurations and self.bridge_id
                else None
            )
            or "",
            "response": response,
            "folder_id": self.folder_id,
            "prompt": self.configuration.get("prompt"),
            "is_cached": is_cached,
            "error": self.tool_call_limit_error or "",
        }

    def service_formatter(self, configuration: object, service: str):  # changes
        try:
            new_config = {
                ServiceKeys[service].get(self.type, ServiceKeys[service]["default"]).get(key, key): value
                for key, value in configuration.items()
            }

            if new_config.get("stream") is not None and service_name[service] in {"anthropic", "gemini", "mistral"}:
                new_config.pop("stream")

            if configuration.get("tools", ""):
                if service == service_name["anthropic"]:
                    new_config["tool_choice"] = configuration.get("tool_choice", {"type": "auto"})
                elif (
                    service == service_name["openai_completion"]
                    or service == service_name["groq"]
                    or service == service_name["grok"]
                ):
                    if configuration.get("tool_choice"):
                        if configuration["tool_choice"] not in ["auto", "none", "required", "default"]:
                            new_config["tool_choice"] = {
                                "type": "function",
                                "function": {"name": configuration["tool_choice"]},
                            }
                        else:
                            new_config["tool_choice"] = configuration["tool_choice"]

                new_config["tools"] = tool_call_formatter(configuration, service, self.variables, self.variables_path)
            elif "tool_choice" in configuration:
                del new_config["tool_choice"]
            if "tools" in new_config and len(new_config["tools"]) == 0:
                del new_config["tools"]
            if service == service_name["openai"] and "text" in new_config:
                data = new_config["text"]
                new_config["text"] = {"format": data}
            
            # Handle Reasoning config 
            if new_config.get("reasoning", False):
                reasoning_formatter(service, new_config)

            if service == service_name['gemini']:
                from google.genai import types

                if 'tools' not in new_config and 'parallel_tool_calls' in new_config:
                    del new_config['parallel_tool_calls']

                # Parallel Tool Config
                if "parallel_tool_calls" in new_config and new_config["parallel_tool_calls"]:
                    new_config["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

                # Flatening JSON Schema
                if "response_mime_type" in new_config and isinstance(new_config['response_mime_type'], dict):
                    match new_config["response_mime_type"]["type"]:
                        case "text":
                            del new_config["response_mime_type"]
                        case "json_object":
                            new_config["response_mime_type"] = "application/json"
                        case "json_schema":
                            new_config["response_json_schema"] = new_config["response_mime_type"]["json_schema"]["schema"]
                            new_config["response_mime_type"] = "application/json" 

                # Build config_params excluding "model", and remove None values
                config_params = {k: v for k, v in new_config.items() if v is not None and k not in {"model", "tool_choice", "parallel_tool_calls", "stream"}}
                
                new_config = {
                    "model": new_config["model"],
                    "config": types.GenerateContentConfig(**config_params)
                }
            
            if service == service_name["deepgram"]:
                if new_config.get("model_option"):
                    new_config["model"] = f"{new_config['model']}-{new_config.pop('model_option')}"

            return new_config
        except Exception as e:
            logger.error(f"An error occurred: {str(e)}")
            error = e.args
            raise ValueError(f"Service key error: {error[0] if error else str(e)}") from e

    async def chats(self, configuration, apikey, service, count=0):
        try:
            response = {}
            loop = asyncio.get_event_loop()
            if service == service_name["openai"]:
                response = await openai_response_model(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                    self.is_embed,
                    self.user_id,
                    self.thread_id,
                    self.playground,
                    self.api_collection,
                )
            elif service == service_name["anthropic"]:
                response = await loop.run_in_executor(
                    executor,
                    lambda: asyncio.run(
                        anthropic_runmodel(
                            configuration,
                            apikey,
                            self.execution_time_logs,
                            self.bridge_id,
                            self.timer,
                            self.name,
                            self.org_name,
                            service,
                            count,
                            self.token_calculator,
                        )
                    ),
                )
            elif service == service_name["groq"]:
                response = await groq_runmodel(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["grok"]:
                response = await grok_runmodel(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["open_router"]:
                response = await openrouter_modelrun(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["mistral"]:
                response = await mistral_model_run(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["gemini"]:
                response = await gemini_modelrun(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["deepgram"]:
                response = await deepgram_runmodel(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                )
            elif service == service_name["openai_completion"]:
                response = await openai_completion(
                    configuration,
                    apikey,
                    self.execution_time_logs,
                    self.bridge_id,
                    self.timer,
                    self.message_id,
                    self.org_id,
                    self.name,
                    self.org_name,
                    service,
                    count,
                    self.token_calculator,
                    self.is_embed,
                    self.user_id,
                    self.thread_id,
                    self.playground,
                    self.api_collection,
                )
            if not response["success"]:
                raise ApiCallError(response["error"], status_code=response.get("status_code"), service=service)
            return {"success": True, "modelResponse": response["response"]}
        except Exception as e:
            logger.error(f"chats error=>, {str(e)}, {traceback.format_exc()}")
            err = ApiCallError(
                f"error occurs from {self.service} api: {e}",
                status_code=getattr(e, "status_code", None),
                service=self.service,
            )
            raise err from e

    async def stream(self, configuration, apikey, service, count=0):
        """Parallel to chats() — streams from the per-service SDK, emits SSE/RTLayer events
        via self.streamer, and returns the same complete response dict as chats()."""
        try:
            if count == 0:
                await self.streamer.emit_start(
                    model=self.model or "",
                    service=service,
                    bridge_id=str(self.bridge_id or ""),
                    message_id=str(self.message_id or ""),
                )

            if service == service_name["openai"]:
                generator = openai_response_stream(configuration, apikey)
            elif service == service_name["anthropic"]:
                generator = anthropic_stream(configuration, apikey)
            elif service == service_name["groq"]:
                generator = groq_stream(configuration, apikey)
            elif service == service_name["grok"]:
                generator = grok_stream(configuration, apikey)
            elif service == service_name["open_router"]:
                generator = openrouter_stream(configuration, apikey)
            elif service == service_name["mistral"]:
                generator = mistral_stream(configuration, apikey)
            elif service == service_name["gemini"]:
                generator = gemini_modelrun_stream(configuration, apikey)
            else:
                raise ApiCallError(f"Streaming not supported for service: {service}", service=service)

            stream_state = await run_stream_and_collect(generator, self.streamer)
            accumulated_content = stream_state["accumulated_content"]
            final_tool_calls = stream_state["final_tool_calls"]
            final_usage = stream_state["final_usage"]
            final_finish_reason = stream_state["final_finish_reason"]
            error_in_stream = stream_state["error_in_stream"]
            last_delta = stream_state["last_delta"]

            if error_in_stream:
                raise ApiCallError(error_in_stream, service=service)

            stream_service_tier = stream_state.get("service_tier")

            accumulated_response = build_accumulated_response(
                service=service,
                configuration=configuration,
                message_id=self.message_id,
                accumulated_content=accumulated_content,
                final_tool_calls=final_tool_calls,
                final_usage=final_usage,
                final_finish_reason=final_finish_reason,
                last_delta=last_delta,
                service_tier=stream_service_tier,
            )

            if self.token_calculator:
                self.token_calculator.calculate_usage(accumulated_response)

            has_tool_calls = bool(final_tool_calls)

            return {"success": True, "modelResponse": accumulated_response, "has_tool_calls": has_tool_calls}

        except ApiCallError:
            raise
        except Exception as e:
            logger.error(f"stream error=>, {str(e)}, {traceback.format_exc()}")
            raise ApiCallError(
                f"error occurs from {self.service} api: {e}",
                status_code=getattr(e, "status_code", None),
                service=self.service,
            ) from e


    async def replace_variables_in_args(self, codes_mapping):
        variables = self.variables
        variables_path = self.variables_path
        if variables_path is None:
            return codes_mapping

        for _key, value in codes_mapping.items():
            args = value.get("args")
            function_name = value.get("name")
            if self.tool_id_and_name_mapping.get(value.get("name"), {}).get("type", "") == "AGENT":
                function_name = self.tool_id_and_name_mapping.get(value.get("name"), {}).get("bridge_id", "")
            else:
                function_name = self.tool_id_and_name_mapping.get(value.get("name"), {}).get("name", value.get("name"))

            if args is not None and function_name in variables_path:
                function_variables_path = variables_path[function_name]
                for path_key, path_value in function_variables_path.items():
                    value_to_set = _.objects.get(variables, path_value)

                    if value_to_set is not None:
                        _.objects.set_(args, path_key, value_to_set)

                value["args"] = args

        return codes_mapping

    async def image(self, configuration, apikey, service):
        try:
            response = {}
            if service == service_name["openai"]:
                response = await OpenAIImageModel(configuration, apikey, self.execution_time_logs, self.timer)
            if service == service_name["gemini"]:
                response = await gemini_image_model(configuration, apikey, self.execution_time_logs, self.timer)
            if not response["success"]:
                raise ValueError(response["error"])
            return {"success": True, "modelResponse": response["response"]}
        except Exception as e:
            logger.error(f"chats error in image=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"error occurs from {self.service} api {e.args[0]}") from e

    async def video(self, configuration, apikey, service):
        try:
            response = {}
            if service == service_name["gemini"]:
                response = await gemini_video_model(
                    configuration, apikey, self.execution_time_logs, self.timer, self.file_data
                )
            if not response["success"]:
                raise ValueError(response["error"])
            return {"success": True, "modelResponse": response["response"]}
        except Exception as e:
            logger.error(f"chats error in video=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"error occurs from {self.service} api {e.args[0]}") from e
