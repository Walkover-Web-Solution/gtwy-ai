import asyncio
import pydash as _
import json
import traceback
from config import Config
from ....db_services import metrics_service
from .utils import validate_tool_call, tool_call_formatter, sendResponse, make_code_mapping_by_service, process_data_and_run_tools
from src.configs.serviceKeys import ServiceKeys
from ..openAI.runModel import openai_response_model, openai_completion
from ..anthropic.anthropicModelRun import anthropic_runmodel
from ..Mistral.mistral_model_run import mistral_model_run
from ....configs.constant import service_name
from ..groq.groqModelRun import groq_runmodel
from ..grok.grokModelRun import grok_runmodel
from ..Google.gemini_modelrun import gemini_modelrun
from ..openRouter.openRouter_modelrun import openrouter_modelrun
from ....configs.constant import service_name
from ..openAI.image_model import OpenAIImageModel
from ..Google.gemini_image_model import gemini_image_model
from ..Google.gemini_video_model import gemini_video_model
from ..AiMl.ai_ml_model_run import ai_ml_model_run
from ..AiMl.ai_ml_image_model import AiMlImageModel
from concurrent.futures import ThreadPoolExecutor
from globals import *


executor = ThreadPoolExecutor(max_workers= int(Config.max_workers) or 10)

class BaseService:
    def __init__(self, params):
        self.customConfig = params.get('customConfig')
        self.configuration = params.get('configuration')
        self.apikey = params.get('apikey')
        self.variables = params.get('variables')
        self.user = params.get('user')
        self.tool_call = params.get('tools')
        self.org_id = params.get('org_id')
        self.bridge_id = params.get('bridge_id')
        self.bridge = params.get('bridge')
        self.thread_id = params.get('thread_id')
        self.sub_thread_id = params.get('sub_thread_id')
        self.model = params.get('model')
        self.service = params.get('service')
        self.modelOutputConfig = params.get('modelOutputConfig')
        self.playground = params.get('playground')
        self.template = params.get('template')
        self.response_format = params.get('response_format')
        self.execution_time_logs = params.get('execution_time_logs',{})
        self.timer = params.get('timer')
        self.func_tool_call_data = []
        self.variables_path = params.get('variables_path')
        self.message_id = params.get('message_id')
        self.bridgeType = params.get('bridgeType')
        self.reasoning_model = params.get('reasoning_model')
        self.memory = params.get('memory')
        self.type = params.get('type')
        self.token_calculator = params.get('token_calculator')
        self.apikey_object_id = params.get('apikey_object_id')
        self.image_data = params.get('images')
        self.audio_data = params.get('audios')
        self.tool_call_count = params.get('tool_call_count')
        self.text = params.get('text')
        self.tool_id_and_name_mapping = params.get('tool_id_and_name_mapping')
        self.batch = params.get('batch')
        self.webhook = params.get('webhook')
        self.batch_variables = params.get('batch_variables')
        self.processed_prompts = params.get('processed_prompts')
        self.name = params.get('name')
        self.org_name = params.get('org_name')
        self.send_error_to_webhook = params.get('send_error_to_webhook')
        self.built_in_tools = params.get('built_in_tools')
        self.function_time_logs = params.get('function_time_logs')
        self.files = params.get('files') or []
        self.file_data = params.get('file_data')
        self.youtube_url = params.get('youtube_url')
        self.web_search_filters = params.get('web_search_filters')
        self.folder_id = params.get('folder_id')
        self.bridge_configurations = params.get('bridge_configurations')
        self.owner_id = params.get('owner_id')


    def aiconfig(self):
        return self.customConfig

    async def run_tool(self, responses, service):
        codes_mapping, function_list = make_code_mapping_by_service(responses, service)
        if not self.playground:
            asyncio.create_task(sendResponse(self.response_format, data = {'function_call': True, 'Name': function_list}, success = True))
        codes_mapping = await self.replace_variables_in_args(codes_mapping)
        
        # Check for transfer action in codes_mapping
        from src.services.utils.transfer_handler import check_transfer_from_codes_mapping
        has_transfer, transfer_config = check_transfer_from_codes_mapping(codes_mapping, self.tool_id_and_name_mapping)
        
        if has_transfer:
            # Return transfer config instead of processing tools
            return [], [], {'transfer_agent_config': transfer_config}
        
        return await process_data_and_run_tools(codes_mapping, self)


    def update_configration(self, response, function_responses, configuration, mapping_response_data, service, tools):    
        if(service == 'anthropic'):
            configuration['messages'].append({'role': 'assistant', 'content': response['content']})
            configuration['messages'].append({'role': 'user','content':[]})

        for index, function_response in enumerate(function_responses):
            tools[function_response['name']] = function_response['content']
        
            match service:
                case 'openai_completion' | 'groq' | 'grok' | 'open_router' | 'mistral' | 'gemini' | 'ai_ml':
                    assistant_tool_calls = response['choices'][0]['message']['tool_calls'][index]
                    configuration['messages'].append({'role': 'assistant', 'content': None, 'tool_calls': [assistant_tool_calls]})
                    tool_calls_id = assistant_tool_calls['id']
                    configuration['messages'].append(mapping_response_data[tool_calls_id])
                case 'openai':
                    # First, add all reasoning outputs to the configuration
                    for output in response['output']:
                        if output.get('type') == 'reasoning':
                            configuration['input'].append(output)
                    
                    # Then handle function calls using the index parameter
                    function_call_outputs = [output for output in response['output'] if output.get('type') == 'function_call']
                    if index < len(function_call_outputs):
                        output = function_call_outputs[index]
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
                case  _:
                    pass
        return configuration, tools

    async def function_call(self, configuration, service, response, l=0, tools={}):
        if not response.get('success'):
            return {'success': False, 'error': response.get('error')}
        
        model_response = response.get('modelResponse', {})
        if configuration.get('tool_choice') is not None and configuration['tool_choice'] not in ['auto', 'none']:
            if service == 'anthropic':
                configuration['tool_choice'] = {'type': 'auto'}
            else:
                configuration['tool_choice'] = 'auto'
        if validate_tool_call(service, model_response) and l <= int(self.tool_call_count or 0):
            l += 1
        else:
            return response
        func_response_data,mapping_response_data, tools_call_data = await self.run_tool(model_response, service)
        self.func_tool_call_data.append(tools_call_data)
        
        # Check if transfer was detected in run_tool
        if isinstance(tools_call_data, dict) and 'transfer_agent_config' in tools_call_data:
            # Return response with transfer config
            response['transfer_agent_config'] = tools_call_data['transfer_agent_config']
            return response
        
        configuration, tools = self.update_configration(model_response, func_response_data, configuration, mapping_response_data, service, tools)
        if not self.playground:
            asyncio.create_task(sendResponse(self.response_format, data = {'function_call': True, 'success': True, 'message': 'Continuing AI reasoning…'}, success=True))
        ai_response = await self.chats(configuration, self.apikey, service, l)
        ai_response['tools'] = tools
        return await self.function_call(configuration, service, ai_response, l, tools)

    async def handle_failure(self, response):
        latency = {
            "over_all_time" : self.timer.stop("Api total time") or "",
            "model_execution_time": sum(self.execution_time_logs.values()) or "",
            "execution_time_logs" : self.execution_time_logs or {}
        }
        usage = {
            'service': self.service,
            'model': self.model,
            'orgId': self.org_id,
            'latency': json.dumps(latency),
            'success': False,
            'error': response.get('error'),
            'apikey_object_id': self.apikey_object_id
        }
        await asyncio.gather(
            metrics_service.create([usage], {
                'thread_id': self.thread_id,
                'sub_thread_id': self.sub_thread_id,
                'user': self.user if self.user else json.dumps(self.tool_call),
                'message': "",
                'org_id': self.org_id,
                'bridge_id': self.bridge_id,
                'model': self.configuration.get('model'),
                'channel': 'chat',
                'type': "error",
                'actor': "user" if self.user else "tool",
                'message_id' : self.message_id
            }, None, self.send_error_to_webhook),
            sendResponse(self.response_format, data=response.get('error')),
            return_exceptions=True
        )
# todo
    def update_model_response(self, model_response, functionCallRes={}):
        funcModelResponse = functionCallRes.get("modelResponse", {})
        if self.service in [
            service_name['openai'],
            service_name['groq'],
            service_name['grok'],
            service_name['anthropic'],
            service_name['open_router'],
            service_name['mistral'],
            service_name['gemini'],
            service_name['ai_ml'],
            service_name['openai_completion']
        ]:

            if funcModelResponse and self.service != service_name['openai']:
                _.set_(model_response, self.modelOutputConfig['message'], _.get(funcModelResponse, self.modelOutputConfig['message']))
                if self.service in [service_name['openai_completion'], service_name['groq'], service_name['grok'], service_name['open_router'], service_name['gemini'], service_name['ai_ml']]:
                    _.set_(model_response, self.modelOutputConfig['tools'], _.get(funcModelResponse, self.modelOutputConfig['tools']))

    def prepare_history_params(self,response, model_response, tools, transfer_agent_config=None):
        # Get the original message content
        original_message = response.get('data',{}).get('content') or ""
        
        # If message is empty but we have transfer config, create custom message
        if not original_message and transfer_agent_config:
            agent_name = transfer_agent_config.get('tool_name', 'the agent')
            original_message = f"Query is successfully transferred to agent {agent_name}"
        
        return {
            'thread_id': self.thread_id,
            'sub_thread_id': self.sub_thread_id,
            'user': self.user if self.user else "",
            'message': original_message,
            'org_id': self.org_id,
            'bridge_id': self.bridge_id,
            'model': model_response.get('model') or self.configuration.get('model'),
            'service': self.service,
            'channel': 'chat',
            'type': "assistant",
            'actor': "user",
            'tools': tools,
            'chatbot_message' : "",
            'tools_call_data' : self.func_tool_call_data,
            'message_id' : self.message_id,
            'llm_urls' : [{'revised_prompt': img.get('revised_prompt'), 'permanent_url': img.get('url'), "type":"image"} for img in model_response.get('data', []) if img.get('url')] or [{'revised_prompt': model_response.get('data',[{}])[0].get('revised_prompt', None), 'permanent_url': model_response.get('data',[{}])[0].get('url', None)}] if model_response.get('data',[{}])[0].get('url') else [],
            'revised_prompt' : model_response.get('data',[{}])[0].get('revised_prompt', None),
            'user_urls': [
                *({'url': u, 'type': 'image'} for u in (self.image_data or [])),
                *({'url': u, 'type': 'pdf'} for u in (self.files or [])),
                *({'url': u, 'type': 'audio'} for u in (self.audio_data or []))
            ],
            'AiConfig' : self.customConfig,
            "firstAttemptError" : model_response.get('firstAttemptError') or '',
            "annotations" : _.get(model_response, self.modelOutputConfig.get('annotations')) or [],
            "fallback_model" : (self.bridge_configurations.get(self.bridge_id, {}).get('fall_back') if self.bridge_configurations and self.bridge_id else None) or '',
            "response":response,
            "folder_id": self.folder_id,
            "prompt": self.configuration.get('prompt')
        }
    
    def service_formatter(self, configuration : object, service : str ):  # changes
        try:
            new_config = {ServiceKeys[service].get(self.type, ServiceKeys[service]['default']).get(key, key): value for key, value in configuration.items()}
            if configuration.get('tools', '') :
                if service == service_name['anthropic']:
                    new_config['tool_choice'] =  configuration.get('tool_choice', {'type': 'auto'})
                elif service == service_name['openai_completion'] or service == service_name['groq'] or service == service_name['grok'] or service == service_name['ai_ml']:
                    if configuration.get('tool_choice'):
                        if configuration['tool_choice'] not in ['auto', 'none', 'required', 'default']:
                            new_config['tool_choice'] = {"type": "function", "function": {"name": configuration['tool_choice']}}
                        else:
                            new_config['tool_choice'] = configuration['tool_choice']
                    
                new_config['tools'] = tool_call_formatter(configuration, service, self.variables, self.variables_path)
            elif 'tool_choice' in configuration:
                del new_config['tool_choice']  
            if 'tools' in new_config and len(new_config['tools']) == 0:
                del new_config['tools']
            if service == service_name['openai'] and 'text' in new_config:
                data = new_config['text']
                new_config['text'] = { "format": data }
            if service == service_name['openai'] and 'reasoning' in new_config:
                # Only transform if reasoning has 'key' and 'type' structure
                if isinstance(new_config['reasoning'], dict) and 'key' in new_config['reasoning'] and 'type' in new_config['reasoning']:
                    new_config['reasoning'] = { new_config['reasoning']['key']: new_config['reasoning']['type'] }
            return new_config
        except Exception as e:
            logger.error(f"An error occurred: {str(e)}")
            raise ValueError(f"Service key error: {e.args[0]}")
        
    async def chats(self, configuration, apikey, service, count=0):
        try:
            response = {}
            loop = asyncio.get_event_loop()
            if service == service_name['openai']:
                response = await openai_response_model(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['anthropic']:
                response = await loop.run_in_executor(executor, lambda: asyncio.run(anthropic_runmodel(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.name, self.org_name, service, count, self.token_calculator)))
            elif service == service_name['groq']:
                response = await groq_runmodel(configuration, apikey, self.execution_time_logs, self.bridge_id,  self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['grok']:
                response = await grok_runmodel(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['open_router']:
                response = await openrouter_modelrun(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['mistral']:
                response = await mistral_model_run(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer,self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['gemini']:
                response = await gemini_modelrun(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['ai_ml']:
                response = await ai_ml_model_run(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            elif service == service_name['openai_completion']:
                response = await openai_completion(configuration, apikey, self.execution_time_logs, self.bridge_id, self.timer, self.message_id, self.org_id, self.name, self.org_name, service, count, self.token_calculator)
            if not response['success']:
                raise ValueError(response['error'])
            return {
                'success': True,
                'modelResponse': response['response']
            }
        except Exception as e:
            logger.error(f"chats error=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"error occurs from {self.service} api {e.args[0]}", *e.args[1:], self.func_tool_call_data)

    async def replace_variables_in_args(self, codes_mapping):
        variables = self.variables
        variables_path = self.variables_path
        if variables_path is None:
            return codes_mapping

        for key, value in codes_mapping.items():
            args = value.get('args')
            function_name = value.get('name')
            if(self.tool_id_and_name_mapping.get(value.get('name'), {}).get('type', '') == 'AGENT'):
                function_name = self.tool_id_and_name_mapping.get(value.get('name'), {}).get('bridge_id', '')
            else:
                function_name = self.tool_id_and_name_mapping.get(value.get('name'), {}).get('name', value.get('name'))

            if args is not None and function_name in variables_path:
                function_variables_path = variables_path[function_name]
                for path_key, path_value in function_variables_path.items():
                    value_to_set = _.objects.get(variables, path_value)

                    if value_to_set is not None:
                        _.objects.set_(args, path_key, value_to_set)

                value['args'] = args

        return codes_mapping

    async def image(self, configuration, apikey, service):
        try:
            response = {}
            if service == service_name['openai']:
                response = await OpenAIImageModel(configuration, apikey, self.execution_time_logs, self.timer)
            if service == service_name['gemini']:
                response = await gemini_image_model(configuration, apikey, self.execution_time_logs, self.timer)
            if service == service_name['ai_ml']:
                response = await AiMlImageModel(configuration, apikey, self.execution_time_logs, self.timer, self.image_data)
            if not response['success']:
                raise ValueError(response['error'])
            return {
                'success': True,
                'modelResponse': response['response']
            }
        except Exception as e:
            logger.error(f"chats error in image=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"error occurs from {self.service} api {e.args[0]}")
        
    async def video(self, configuration, apikey, service):
        try:
            response = {}
            if service == service_name['gemini']:
                response = await gemini_video_model(configuration, apikey, self.execution_time_logs, self.timer, self.file_data)
            if not response['success']:
                raise ValueError(response['error'])
            return {
                'success': True,
                'modelResponse': response['response']
            }
        except Exception as e:
            logger.error(f"chats error in video=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"error occurs from {self.service} api {e.args[0]}")