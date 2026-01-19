import hashlib
from Crypto.Cipher import AES
import pydash as _
import json
from config import Config
from Crypto.Util.Padding import unpad
import traceback    
from functools import reduce
import operator
import re
from src.configs.model_configuration import model_config_document
import jwt
from ..commonServices.openAI.openai_batch import OpenaiBatch
from ..commonServices.Google.gemini_batch import GeminiBatch
from ..commonServices.anthropic.anthropic_batch import AnthropicBatch
from ..commonServices.groq.groq_batch import GroqBatch
from ..commonServices.Mistral.mistral_batch import MistralBatch
from ..commonServices.openAI.openai_response import OpenaiResponse
from ..commonServices.groq.groqCall import Groq
from ..commonServices.grok.grokCall import Grok
from ..commonServices.anthropic.anthropicCall import Anthropic
from ..commonServices.openRouter.openRouter_call import OpenRouter
from ..commonServices.Mistral.mistral_call import Mistral
from ...configs.constant import service_name
from ..commonServices.openAI.openai_embedding_call import OpenaiEmbedding
from ..commonServices.Google.geminiCall import GeminiHandler
from ..commonServices.AiMl.ai_ml_call import Ai_Ml
from ..commonServices.openAI.openai_completion_response import OpenaiCompletion
from ..cache_service import find_in_cache, store_in_cache
from ..utils.apiservice import fetch
from ..commonServices.baseService.utils import sendResponse
from datetime import datetime
import pytz
from src.configs.constant import redis_keys
class Helper:
    @staticmethod
    def encrypt(text):
        iv = hashlib.sha512(Config.Secret_IV.encode()).hexdigest()[:16]
        key = hashlib.sha512(Config.Encreaption_key.encode()).hexdigest()[:32]
        cipher = AES.new(key.encode(), AES.MODE_CFB, iv.encode())
        encrypted = cipher.encrypt(text.encode())
        return encrypted.hex()

    @staticmethod
    def decrypt(encrypted_text):
        token = None
        encryption_key=Config.Encreaption_key
        secret_iv=Config.Secret_IV
        
        iv = hashlib.sha512(secret_iv.encode()).hexdigest()[:16]
        key = hashlib.sha512(encryption_key.encode()).hexdigest()[:32]

        encrypted_text_bytes = bytes.fromhex(encrypted_text)
        try:
            # Attempt to decrypt using AES CBC mode
            cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
            decrypted_bytes = unpad(cipher.decrypt(encrypted_text_bytes), AES.block_size)
            token = decrypted_bytes.decode('utf-8')
        except (ValueError, KeyError) as e:
            # Attempt to decrypt using AES CFB mode
            cipher = AES.new(key.encode(), AES.MODE_CFB, iv.encode())
            decrypted_bytes = cipher.decrypt(encrypted_text_bytes)
            token = decrypted_bytes.decode('utf-8')
        return token
    
    @staticmethod 
    def mask_api_key(key):
        if not key:
            return ''
        if len(key) > 6:
            return key[:3] + '*' * (9) + key[-3:]
        return key

    @staticmethod
    def extract_embed_user_id(userinfo, org_id):
        if not userinfo:
            return None

        data = userinfo.get("data") if isinstance(userinfo, dict) else userinfo
        if not isinstance(data, list) or not data:
            return None

        first_entry = data[0]
        if isinstance(first_entry, dict):
            mail = first_entry.get("email")
        else:
            mail = getattr(first_entry, "email", None)

        if not isinstance(mail, str) or not mail:
            return None

        cleaned_mail = mail
        if org_id:
            cleaned_mail = cleaned_mail.removeprefix(org_id)
        cleaned_mail = cleaned_mail.removesuffix("@gtwy.ai")

        return cleaned_mail or None
         

    @staticmethod
    def update_configuration(prev_configuration, configuration):
        for key in prev_configuration:
            prev_configuration[key] = configuration.get(key, prev_configuration[key])
        for key in configuration:
            prev_configuration[key] = configuration[key]
        if "tools" in prev_configuration and len(prev_configuration["tools"]) == 0:
            del prev_configuration["tools"]
        return prev_configuration

    @staticmethod
    def replace_variables_in_prompt(prompt, Aviliable_variables):
        missing_variables = {}
        placeholders = re.findall(r'\{\{(.*?)\}\}', prompt)
        flattened_json = Helper.custom_flatten(Aviliable_variables)
        variables = {**Aviliable_variables, **flattened_json}

        if variables:
            for key, value in variables.items():
                if key in placeholders:
                    string_value = str(value)
                    string_value = string_value[1:-1] if string_value.startswith('"') and string_value.endswith('"') else string_value
                    string_value = string_value.replace("\\", "\\\\")
                    regex = re.compile(r'\{\{' + re.escape(key) + r'\}\}')
                    prompt = regex.sub(string_value, prompt)
                    placeholders.remove(key)

        for placeholder in placeholders:
            missing_variables[placeholder] = f"{{{{{placeholder}}}}}"

        return prompt, missing_variables


    @staticmethod
    def custom_flatten(d, parent_key='', sep='.'):
        """
        Flattens a dictionary and preserves nested structures.
        :param d: Dictionary to flatten
        :param parent_key: The base key string
        :param sep: Separator between keys
        :return: A flattened dictionary with nested structures preserved
        """
        items = {}
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                # Add the current nested structure as a whole
                items[new_key] = v
                # Flatten recursively
                items.update(Helper.custom_flatten(v, new_key, sep=sep))
            else:
                items[new_key] = v
        return items

    @staticmethod
    def parse_json(json_string):
        try:
            return {"success": True, "json": json.loads(json_string)}
        except json.JSONDecodeError as error:
            return {"success": False, "error": str(error)}

    def get_value_from_location(obj, location):
        try:
            keys = location.split(".")
            keys = [int(key) if key.isdigit() else key for key in keys]
            
            value = reduce(operator.getitem, keys, obj)
            return value
        except Exception as e:
            traceback.print_exc()
            return None
    def generate_token(payload, accesskey):
        return jwt.encode(payload, accesskey)

    async def response_middleware_for_bridge (service, finalResponse, isBridgeUpdated = False):
        try:
            response = finalResponse['bridge']
            model = response['configuration']['model']
            modelObj = model_config_document[service][model]
            configurations = modelObj['configuration']
            db_config = response['configuration']
            org_id = response['org_id']
            bridge_id = response.get('parent_id') if response.get('parent_id') else response['_id']
            version_id = None if not response.get('parent_id') else response['_id']
            # if response.get('apikey'):
            #     decryptedApiKey = Helper.decrypt(response['apikey'])
            #     maskedApiKey = Helper.mask_api_key(decryptedApiKey)
            #     response['apikey'] = maskedApiKey
            config = {}
            for key in configurations.keys():
                config[key] = db_config.get(key, response['configuration'].get(key, configurations[key].get("default", '')))
            for key in ['prompt','response_format','type', 'pre_tools','fine_tune_model', 'is_rich_text','tone','responseStyle']:
                if key == 'response_format':
                    config[key] = db_config.get(key, response['configuration'].get(key, {"type":'default',"cred":{}}))
                elif key == 'fine_tune_model':
                    config[key] = db_config.get(key, response['configuration'].get(key, {}))
                elif key == 'type':
                    config[key] = db_config.get(key, response['configuration'].get(key, 'chat'))
                elif key == 'pre_tools':
                    config[key] = db_config.get(key, response['configuration'].get(key, []))
                elif key == 'is_rich_text':
                    config[key] = db_config.get(key, response['configuration'].get(key, True))
                else:
                    config[key] = db_config.get(key, response['configuration'].get(key, ''))
            response['configuration'] = config
            finalResponse['bridge'] = response

            response_format_copy = {
                'cred' : {
                    'channel': org_id + bridge_id,
                    'apikey': Config.RTLAYER_AUTH,
                    'ttl': '1'
                },
                'type' : 'RTLayer'
            }
            dataToSend={
                'type': "agent_updated",
                'bridge_id': bridge_id,
                'version_id': version_id
            }
            if isBridgeUpdated:
                await sendResponse(response_format_copy, dataToSend, True)
            return finalResponse
        except json.JSONDecodeError as error:
            return {"success": False, "error": str(error)}
        
    def find_variables_in_string(prompt):
        variables = re.findall(r'{{(.*?)}}', prompt)
        return variables
    
    async def create_service_handler(params, service):
        class_obj = None
        if service == service_name['openai']:
            class_obj = OpenaiResponse(params)
        elif service == service_name['gemini']:
            class_obj = GeminiHandler(params)
        elif service == service_name['anthropic']:
            class_obj = Anthropic(params)
        elif service == service_name['groq']:
            class_obj = Groq(params)
        elif service == service_name['grok']:
            class_obj = Grok(params)
        elif service == service_name['open_router']:
            class_obj = OpenRouter(params)
        elif service == service_name['mistral']:
            class_obj = Mistral(params)
        elif service == service_name['ai_ml']:
            class_obj = Ai_Ml(params)
        elif service == service_name['openai_completion']:
            class_obj = OpenaiCompletion(params)
        else:
            raise ValueError(f"Unsupported service: {service}")
        
        return class_obj

    
    def calculate_usage(model, model_response, service):
        usage = {}
        token_cost = {}
        permillion = 1000000
        modelObj = model_config_document[service][model]
        if modelObj is None:
            raise AttributeError(f"Model function '{model}' not found in model_configuration.")

        if service in ['openai', 'groq', 'grok', 'ai_ml', 'openai_completion']:
            token_cost['input_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('input_cost') or 0
            token_cost['output_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('output_cost') or 0
            token_cost['cache_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('cached_cost') or 0
            
            usage["inputTokens"] = _.get(model_response['usage'], 'input_tokens', 0)
            usage["outputTokens"] = _.get(model_response['usage'], 'output_tokens', 0)
            usage["cachedTokens"] = _.get(model_response['usage'], 'cached_token') or 0

            usage["expectedCost"] = 0
            if usage["inputTokens"]:
                usage["expectedCost"] += usage['inputTokens'] * (token_cost['input_cost'] / permillion)
            if usage["outputTokens"]:
                usage["expectedCost"] += usage['outputTokens'] * (token_cost['output_cost'] / permillion)
            if usage["cachedTokens"]:
                usage["expectedCost"] += usage['cachedTokens'] * (token_cost['cache_cost'] / permillion)

        elif service == 'anthropic':
            # model_specific_config = model_response['usage'][0].get('total_cost', {}).get(model, {})
            usage["inputTokens"] = _.get(model_response['usage'], 'input_tokens', 0)
            usage["outputTokens"] = _.get(model_response['usage'], 'output_tokens', 0)
            usage["cachedCreationInputTokens"] = _.get(model_response['usage'], 'cache_creation_input_tokens') or 0
            usage["cachedReadInputTokens"] = _.get(model_response['usage'], 'cache_read_input_tokens') or 0

            token_cost['input_cost'] = modelObj['outputConfig']['usage'][0]['total_cost']['input_cost']
            token_cost['output_cost'] = modelObj['outputConfig']['usage'][0]['total_cost']['output_cost']
            token_cost['cached_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('cached_cost') or 0
            token_cost['caching_write_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('caching_write_cost') or 0
            token_cost['caching_read_cost'] = modelObj['outputConfig']['usage'][0]['total_cost'].get('caching_read_cost') or 0

            usage["expectedCost"] = 0
            if usage["inputTokens"]:
                usage["expectedCost"] += usage['inputTokens'] * (token_cost['input_cost'] / permillion)
                usage["expectedCost"] += usage['inputTokens'] * (token_cost['caching_read_cost'] / permillion)
                usage["expectedCost"] += usage['cachedCreationInputTokens'] * (token_cost['caching_read_cost'] / permillion) 

            if usage["outputTokens"]:
                usage["expectedCost"] += usage['outputTokens'] * (token_cost['output_cost'] / permillion)
                usage["expectedCost"] += usage['cachedReadInputTokens'] * (token_cost['caching_write_cost'] / permillion)

        return usage
    
    async def create_service_handler_for_batch(params, service):
        # Currently only supports openai and anthropic
        class_obj = None
        if service == service_name['openai']:
            class_obj = OpenaiBatch(params)
        elif service == service_name['anthropic']:
            class_obj = AnthropicBatch(params)
        else:
            raise ValueError(f"Unsupported batch service: {service}")
            
        return class_obj

    async def embedding_service_handler(params, service):
        class_obj = None
        if service == service_name['openai']:
            class_obj = OpenaiEmbedding(params)
        else:
            raise ValueError(f"Unsupported embedding service: {service}")
        return class_obj
    
    def add_doc_description_to_prompt(prompt, rag_data):
        prompt += '\n Available Knowledge Base :- Here are the available documents to get data when needed call the function get_knowledge_base_data: \n'
        
        for idx, data in enumerate(rag_data, 1):
            # Skip if data is not a dictionary
            if not isinstance(data, dict):
                continue
                
            resource_id = data.get('resource_id', '')
            resource_description = data.get('description', 'No description available')
            
            prompt += f"{idx}. Resource ID: {resource_id}\n"
            prompt += f"   Description: {resource_description}\n\n"
        
        return prompt
    
    def append_tone_and_response_style_prompts(prompt, tone, response_style):
        if tone:
            prompt += f"\n\nTone Prompt: {tone['prompt']}"
        if response_style:
            prompt += f"\n\nResponse Style Prompt: {response_style['prompt']}"
        return prompt
      
    # Removed MSG proxy delegation methods: use src.services.proxy.Proxyservice directly

    

    def sort_bridges(bridges, metrics_data):
        # Create a dictionary to map _id to total tokens
        token_map = {_id: tokens for _id, tokens in metrics_data}
        
        # Split bridges into those with and without metrics data
        present = []
        not_present = []
        for bridge in bridges:
            if bridge['_id'] in token_map:
                bridge['total_tokens'] = token_map[bridge['_id']]
                present.append(bridge)

            else:
                bridge['total_tokens'] = 0
                not_present.append(bridge)
        
        # Sort the present bridges by descending token count
        # present.sort(key=lambda x: token_map[x['_id']], reverse=True)
        
        # Combine the lists, keeping not_present bridges in their original order at the end
        return present + not_present
    


    def get_current_time_with_timezone(tz_identifier):
        try:
            tz = pytz.timezone(tz_identifier)
            offset = datetime.now(tz).utcoffset()
            hours, remainder = divmod(offset.total_seconds(), 3600)
            minutes = remainder // 60
            return int(hours), int(minutes)
        except Exception as e:
            return f"Invalid timezone: {e}"
        
    def get_req_opt_variables_in_prompt(prompt, variable_state, variable_path):
        def flatten_values_only(d):
            result = {}
            for value in d.values():  # Ignore top-level keys
                if isinstance(value, dict):
                    result.update(flatten_dict(value))
            return result

        def flatten_dict(d, parent_key=''):
            flat = {}
            for k, v in d.items():
                new_key = f'{parent_key}.{k}' if parent_key else k
                if isinstance(v, dict):
                    flat.update(flatten_dict(v, new_key))
                else:
                    flat[new_key] = "required"
            return flat

        # Extract variables from prompt
        prompt_vars = re.findall(r'{{(.*?)}}', prompt)

        # Determine status for prompt variables based on new structure
        final = {}
        for var in prompt_vars:
            if var in variable_state and isinstance(variable_state[var], dict):
                # Use the status from the variable_state structure
                var_status = variable_state[var].get('status', 'optional')
                final[var] = var_status
            else:
                # Default to optional if not found in variable_state
                final[var] = 'optional'

        # Add flattened variable_path keys as required
        for path in flatten_values_only(variable_path):
            final[path] = 'required'

        return final

    def transform_agent_variable_to_tool_call_format(input_data):
        fields = {}
        required_params = []

        def set_nested_value(obj, path, value, is_required):
            parts = path.split('.')
            current = obj

            for i in range(len(parts) - 1):
                part = parts[i]

                if part not in current:
                    current[part] = {
                        "type": "object",
                        "description": "",
                        "enum": [],
                        "required_params": [],
                        "parameter": {}
                    }
                elif "parameter" not in current[part]:
                    current[part]["parameter"] = {}

                current = current[part]["parameter"]

            final_key = parts[-1]

            # Infer type
            param_type = "string"
            if "number" in final_key.lower() or "num" in final_key.lower():
                param_type = "number"
            elif "bool" in final_key.lower() or "flag" in final_key.lower():
                param_type = "boolean"

            current[final_key] = {
                "type": param_type,
                "description": "",
                "enum": [],
                "required_params": []
            }

            if is_required:
                for i in range(len(parts) - 1):
                    current_level = obj
                    for j in range(i):
                        current_level = current_level[parts[j]]["parameter"]

                    parent_key = parts[i]
                    child_key = parts[i + 1]

                    if child_key not in current_level[parent_key]["required_params"]:
                        current_level[parent_key]["required_params"].append(child_key)

                if parts[0] not in required_params:
                    required_params.append(parts[0])

        for key, value in input_data.items():
            is_required = value == "required"

            if '.' in key:
                set_nested_value(fields, key, value, is_required)
            else:
                param_type = "string"
                if "number" in key.lower() or "num" in key.lower():
                    param_type = "number"
                elif "bool" in key.lower() or "flag" in key.lower():
                    param_type = "boolean"

                fields[key] = {
                    "type": param_type,
                    "description": "",
                    "enum": [],
                    "required_params": []
                }

                if is_required and key not in required_params:
                    required_params.append(key)

        return {
            "fields": fields,
            "required_params": required_params
        }

    
