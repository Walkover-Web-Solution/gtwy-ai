import asyncio
import json
import traceback
from concurrent.futures import ThreadPoolExecutor

import pydash as _

from config import Config
from globals import logger
from src.configs.serviceKeys import ServiceKeys
from src.configs.service_registry import has_openai_choices_shape, supports_tool_calls, uses_openai_sdk

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
from ..openAI.runModel import openai_response_model, openai_response_stream
from ..openAI.openai_stream_utils import sanitize_openai_response_item
from ..openaiCompatible.openai_compatible_modelrun import openai_compatible_modelrun, openai_compatible_stream
from ..streaming_service import StreamingService
from .utils import (
    build_accumulated_response,
    make_code_mapping_by_service,
    process_data_and_run_tools,
    run_stream_and_collect,
    sendResponse,
    tool_call_formatter,
    validate_tool_call,
    reasoning_formatter,
    disable_tool_call
)
from src.services.utils.mcp_utils import (
    MCP_NAME_SUFFIX,
    client_mcp_config,
    display_mcp_tool_name,
    extract_server_side_mcp_calls,
    resolve_mcp_type,
    server_mcp_config,
)
from src.services.utils.maximum_iterations_utils import build_tool_count_key, decrement_tool_count, get_tool_count
from src.exceptions import ApiCallError

executor = ThreadPoolExecutor(max_workers=int(Config.max_workers) or 10)

# Same-model retries for an empty / prematurely-dropped stream, attempted
# inside stream() BEFORE the model-level fallback in sse_stream_and_finalize.
STREAM_SAME_MODEL_MAX_RETRIES = 1


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
        self.template = params.get("template")
        self.response_format = params.get("response_format")
        self.thread_flag = params.get("thread_flag")
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
        self.meta = params.get("meta")
        self.created_at = params.get("created_at")
        self.tool_call_limit_error = None
        self.maximum_iteration_limit_reached = False
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
        if self.response_format.get("type", "") != "webhook":
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

        func_response_data, mapping_response_data, tools_call_data = await process_data_and_run_tools(codes_mapping, self)

        if self.response_format.get("type", "") != "webhook":
            asyncio.create_task(
                sendResponse(
                    self.response_format,
                    data={"function_call": True, "Name": function_list, "result": func_response_data},
                    success=True,
                )
            )

        return func_response_data, mapping_response_data, tools_call_data

    def update_configration(self, response, function_responses, configuration, mapping_response_data, service, tools):
        if service == "anthropic":
            configuration["messages"].append({"role": "assistant", "content": response["content"]})
            configuration["messages"].append({"role": "user", "content": []})

        for index, function_response in enumerate(function_responses):
            display_tool_name = display_mcp_tool_name(function_response["name"])
            tools[display_tool_name] = function_response["content"]

            match service:
                case s if has_openai_choices_shape(s):  # openai_chat wire format (choices[0].message)
                    assistant_tool_calls = response['choices'][0]['message']['tool_calls'][index]
                    assistant_msg = {'role': 'assistant', 'content': None, 'tool_calls': [assistant_tool_calls]}
                    # Moonshot requires reasoning_content in assistant messages when thinking mode is enabled
                    reasoning_content = response['choices'][0]['message'].get('reasoning_content')
                    if reasoning_content is not None:
                        assistant_msg['reasoning_content'] = reasoning_content
                    configuration['messages'].append(assistant_msg)
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
                        candidate_content = response.get('candidates', [{}])[0].get('content', {})
                        if isinstance(candidate_content, dict):
                            for part in candidate_content.get('parts', []) or []:
                                function_call = part.get('function_call') if isinstance(part, dict) else None
                                if isinstance(function_call, dict):
                                    synthetic_id = function_call.get('id')
                                    if isinstance(synthetic_id, str) and synthetic_id.startswith('gemini_fc_'):
                                        function_call.pop('id', None)
                        configuration['contents'].append(candidate_content)

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
        tool_count_key = build_tool_count_key(self.bridge_id, self.message_id)

        if not response.get("success"):
            return {"success": False, "error": response.get("error")}

        model_response = response.get("modelResponse", {})
        if configuration.get("tool_choice") is not None and configuration["tool_choice"] not in ["auto", "none"]:
            if service == "anthropic":
                configuration["tool_choice"] = {"type": "auto"}
            else:
                configuration["tool_choice"] = "auto"
        if not validate_tool_call(service, model_response):
            return response

        loop_count += 1
        remaining = decrement_tool_count(tool_count_key)
        if remaining <= 0:
            disable_tool_call(configuration, service)
            self.maximum_iteration_limit_reached = True
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
                tool_result_name = tool_result.get("name", "")
                is_mcp_result = tool_result_name.endswith(MCP_NAME_SUFFIX)
                await self.streamer.emit_tool_result(
                    name=display_mcp_tool_name(tool_result_name),
                    content=tool_result.get("content", ""),
                    call_id=tool_result.get("tool_call_id", ""),
                    type="mcp" if is_mcp_result else None,
                )

        configuration, tools = self.update_configration(
            model_response, func_response_data, configuration, mapping_response_data, service, tools
        )
        if not self.stream_mode and self.response_format.get("type", "") != "webhook":
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
        history_data = payload["conversation_log_data"]
        history_data["thread_flag"] = self.thread_flag
        history_data["response_format"] = self.response_format
        await asyncio.gather(
            sub_queue_obj.publish_message(make_json_serializable({"save_history": [history_data]})),
            sendResponse(self.response_format, data=response.get("error"), meta=self.meta),
            return_exceptions=True,
        )

    # todo
    def update_model_response(self, model_response, functionCallRes=None):
        if functionCallRes is None:
            functionCallRes = {}
        funcModelResponse = functionCallRes.get("modelResponse", {})
        if supports_tool_calls(self.service):
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
                    service_name["deepseek"],
                    service_name["open_router"],
                    service_name["neev_cloud"],
                    service_name["moonshot"],
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

        server_side_mcp = extract_server_side_mcp_calls(self.service, model_response)
        tools_call_data = list(self.func_tool_call_data or [])
        if server_side_mcp:
            tools_call_data.append(server_side_mcp)

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
            "tools_call_data": tools_call_data,
            "message_id": self.message_id,
            "llm_urls": (
                [
                    {"revised_prompt": img.get("revised_prompt"), "permanent_url": img.get("url"), "type": "image"}
                    for img in model_response.get("data", [])
                    if img.get("url")
                ]
                + [
                    {
                        "revised_prompt": item.get("revised_prompt"),
                        "permanent_url": item.get("image_url") or item.get("permanent_url") or item.get("url"),
                        "type": "image",
                    }
                    for item in model_response.get("output", [])
                    if isinstance(item, dict)
                    and item.get("type") == "image_generation_call"
                    and (item.get("image_url") or item.get("permanent_url") or item.get("url"))
                ]
            ),
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
            "error": "",
            "plans": self.parsed_data.get("plans") if hasattr(self, 'parsed_data') else None,
            "created_at": self.created_at,
        }

    def service_formatter(self, configuration: object, service: str):  # changes
        try:
            new_config = {
                ServiceKeys[service].get(self.type, ServiceKeys[service]["default"]).get(key, key): value
                for key, value in configuration.items()
            }

            if new_config.get("stream") is not None and service_name[service] in {"anthropic", "gemini", "mistral"}:
                new_config.pop("stream")

            mcp_config = self.configuration.get("mcp_config") if isinstance(self.configuration, dict) else None
            mcp_active = bool(mcp_config and mcp_config.get("servers"))
            mcp_type = resolve_mcp_type(service, self.model) if mcp_active else None
            if mcp_active and mcp_type == "client":
                client_mcp_config(service, configuration, mcp_config, self.tool_id_and_name_mapping)

            if configuration.get("tools", ""):
                if service == service_name["anthropic"]:
                    new_config["tool_choice"] = configuration.get("tool_choice", {"type": "auto"})
                elif (
                    service == service_name["openai_completion"]
                    or service == service_name["groq"]
                    or service == service_name["grok"]
                    or service == service_name["deepseek"]
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
            if service == service_name["openai"]:
                # The Responses API takes the system prompt as a developer/system message
                # inside `input`. A top-level string `prompt` is invalid here (the param is
                # reserved for prompt-template objects), so drop a stray string prompt.
                if isinstance(new_config.get("prompt"), str):
                    new_config.pop("prompt", None)

                if "text" in new_config:
                    data = new_config["text"]
                    if isinstance(data, dict):
                        rtype = data.get("type")
                        fmt = {"type": rtype} if rtype else dict(data)
                        if rtype == "json_schema":
                            # json_schema format fields may live flattened alongside `type`
                            # (restructure_json_schema) or still nested under `json_schema`.
                            for k in ("name", "schema", "strict", "description"):
                                if data.get(k) is not None:
                                    fmt[k] = data[k]
                            nested = data.get("json_schema")
                            if isinstance(nested, dict):
                                for k, v in nested.items():
                                    if v is not None:
                                        fmt[k] = v
                        new_config["text"] = {"format": fmt}
                    else:
                        new_config["text"] = {"format": data}

            if new_config.get("verbosity") and service == service_name["openai"]:
                verbosity = new_config.pop("verbosity", {})

                if isinstance(verbosity, dict) and "level" in verbosity:
                    new_config.setdefault("text", {})["verbosity"] = verbosity["level"]

            # Handle Reasoning config 
            if new_config.get("reasoning", False):
                reasoning_formatter(service, new_config)

            if mcp_active and mcp_type == "server":
                server_mcp_config(service, new_config, mcp_config)

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

            remaining = get_tool_count(build_tool_count_key(self.bridge_id, self.message_id))
            if remaining is not None and remaining == 0:
                disable_tool_call(new_config, service)
                self.maximum_iteration_limit_reached = True

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
            elif uses_openai_sdk(service):
                # open_router / neev_cloud / moonshot / openai_completion (+ any future
                # openai_sdk service) share one AsyncOpenAI runner; base_url + flags
                # come from the registry.
                response = await openai_compatible_modelrun(
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
                    self.api_collection,
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

            # Retry the SAME model on an empty / prematurely-dropped stream before
            # the model-level fallback (in sse_stream_and_finalize) ever kicks in.
            # emit_start above is intentionally outside this loop so a retry never
            # re-emits the SSE start event.
            for stream_attempt in range(STREAM_SAME_MODEL_MAX_RETRIES + 1):
                # Fresh generator each attempt => fresh provider request.
                if service == service_name["openai"]:
                    generator = openai_response_stream(configuration, apikey)
                elif service == service_name["anthropic"]:
                    generator = anthropic_stream(configuration, apikey)
                elif service == service_name["groq"]:
                    generator = groq_stream(configuration, apikey)
                elif service == service_name["grok"]:
                    generator = grok_stream(configuration, apikey)
                elif uses_openai_sdk(service):
                    # open_router / neev_cloud / moonshot (+ any future openai_sdk service)
                    generator = openai_compatible_stream(configuration, apikey, service)
                elif service == service_name["mistral"]:
                    generator = mistral_stream(configuration, apikey)
                elif service == service_name["gemini"]:
                    generator = gemini_modelrun_stream(configuration, apikey)
                else:
                    raise ApiCallError(f"Streaming not supported for service: {service}", service=service)

                if self.timer:
                    self.timer.start()
                _stream_exc = None
                try:
                    stream_state = await run_stream_and_collect(generator, self.streamer)
                except Exception as _exc:
                    _stream_exc = _exc
                finally:
                    if self.timer and self.timer.start_times:
                        _elapsed = self.timer.stop(f"{service} stream")
                        if _stream_exc is None:
                            self.execution_time_logs.append({
                                "step": f"{service} stream time for call :- {count + 1} (attempt {stream_attempt + 1})",
                                "time_taken": round(_elapsed, 4),
                            })
                # A genuinely raised exception is re-raised as before (not retried);
                # for OpenAI, real drops/timeouts surface as error_in_stream instead.
                if _stream_exc is not None:
                    raise _stream_exc
                accumulated_content = stream_state["accumulated_content"]
                accumulated_reasoning = stream_state.get("accumulated_reasoning", [])
                final_tool_calls = stream_state["final_tool_calls"]
                final_usage = stream_state["final_usage"]
                final_finish_reason = stream_state["final_finish_reason"]
                error_in_stream = stream_state["error_in_stream"]
                last_delta = stream_state["last_delta"]

                # Did anything reach the client yet? content/reasoning/tool_calls are
                # accumulated in lock-step with their streamer emits, so this is a
                # reliable "nothing streamed yet" signal.
                emitted_any = bool(accumulated_content or accumulated_reasoning or final_tool_calls)

                # An incomplete stream (e.g. the provider closed the connection before
                # sending its completion/usage event) yields nothing at all.
                is_empty = (
                    not accumulated_content
                    and not accumulated_reasoning
                    and not final_tool_calls
                    and not final_finish_reason
                    and not final_usage
                )

                # Retry the no-data case, and pre-emission connection drops — but never
                # after partial output was streamed (avoids duplicate tokens to client).
                is_retryable = is_empty or (error_in_stream and not emitted_any)
                if is_retryable and stream_attempt < STREAM_SAME_MODEL_MAX_RETRIES:
                    logger.warning(
                        f"{service} stream incomplete (empty={is_empty}, "
                        f"error_in_stream={bool(error_in_stream)}), retrying same model "
                        f"(attempt {stream_attempt + 1}/{STREAM_SAME_MODEL_MAX_RETRIES})"
                    )
                    continue

                if error_in_stream:
                    raise ApiCallError(error_in_stream, service=service)

                # Attempts exhausted and still empty — treat as a failure instead of
                # returning an empty "success" response.
                if is_empty:
                    logger.error(
                        f"{service} stream returned no data — incomplete stream. last_delta keys="
                        f"{list(last_delta.keys()) if isinstance(last_delta, dict) else type(last_delta).__name__}, "
                        f"last_delta={str(last_delta)[:500]}"
                    )
                    raise ApiCallError(
                        f"{service} stream returned no data (incomplete stream — no completion event received)",
                        service=service,
                    )

                break  # success — exit retry loop with final stream_state in scope

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
                accumulated_reasoning=accumulated_reasoning,
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

        if not variables_path or not variables:
            return codes_mapping

        tool_mapping_dict = self.tool_id_and_name_mapping

        for value in codes_mapping.values():
            args = value.get("args")
            if not isinstance(args, dict):
                continue

            tool_mapping = tool_mapping_dict.get(value.get("name"), {})
            function_name = (
                tool_mapping.get("bridge_id")
                if tool_mapping.get("type") == "AGENT"
                else tool_mapping.get("name", value.get("name"))
            )

            function_variables_path = variables_path.get(function_name)
            if not function_variables_path:
                continue

            for path_key, path_value in function_variables_path.items():
                value_to_set = _.objects.get(variables, path_value)
                if value_to_set is None:
                    continue

                keys = path_key.split('.')
                current = args
                for key in keys[:-1]:
                    next_node = current.get(key)
                    if not isinstance(next_node, dict):
                        current[key] = {}
                        next_node = current[key]
                    current = next_node
                current[keys[-1]] = value_to_set

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
