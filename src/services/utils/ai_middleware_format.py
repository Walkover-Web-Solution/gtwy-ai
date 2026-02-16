import json
from config import Config
from src.services.utils.apiservice import fetch
from src.configs.constant import service_name

async def Response_formatter(response = {}, service = None, tools={}, type='chat', images = None, isBatch = False):
    tools_data = tools
    if isinstance(tools_data, dict):
                for key, value in tools_data.items():
                    if isinstance(value, str):
                        try:
                            tools_data[key] = json.loads(value)
                        except json.JSONDecodeError:
                            pass
    if (service == 'openai' or service == 'groq' or service == 'mistral') and isBatch:
        # Batch responses for OpenAI-compatible services (OpenAI, Groq, Mistral)
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None),
                "cached_tokens" : response.get("usage", {}).get("prompt_tokens_details",{}).get('cached_tokens')

            }
        }
    elif service == 'gemini' and isBatch:
        # Gemini batch responses have a different structure
        candidates = response.get("candidates", [{}])
        content_parts = candidates[0].get("content", {}).get("parts", [{}]) if candidates else [{}]
        
        return {
            "data" : {
                "id" : response.get("key", None),  # Use the key from batch response as ID
                "content" : content_parts[0].get("text", None) if content_parts else None,
                "model" : response.get("modelVersion", None),
                "role" : candidates[0].get("content", {}).get("role", "model") if candidates else "model",
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : None,
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(candidates[0].get("finishReason", "") if candidates else "")
            },
            "usage" : {
                "input_tokens" : response.get("usageMetadata", {}).get("promptTokenCount", None),
                "output_tokens" : response.get("usageMetadata", {}).get("candidatesTokenCount", None),
                "total_tokens" : response.get("usageMetadata", {}).get("totalTokenCount", None),
                "cached_tokens" : response.get("usageMetadata", {}).get("cachedContentTokenCount", None)
            }
        }
    elif service == 'anthropic' and isBatch:
        # Anthropic batch responses follow standard Anthropic message format
        content_blocks = response.get("content", [])
        text_content = next((block.get("text") for block in content_blocks if block.get("type") == "text"), None)
        
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : text_content,
                "model" : response.get("model", None),
                "role" : response.get("role", "assistant"),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : None,
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("stop_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("input_tokens", None),
                "output_tokens" : response.get("usage", {}).get("output_tokens", None),
                "total_tokens" : (
                    response.get("usage", {}).get("input_tokens", 0) + 
                    response.get("usage", {}).get("output_tokens", 0)
                ),
                "cache_read_input_tokens" : response.get("usage", {}).get("cache_read_input_tokens", None),
                "cache_creation_input_tokens" : response.get("usage", {}).get("cache_creation_input_tokens", None)
            }
        }
    elif service == service_name['openai'] and (type != 'image' and type != 'embedding'):
        return {
            "data": {
                "id": response.get("id", None),
                "content": (
                    # Check if any item in output is a function call
                    next(
                        (f"Function call: {item.get('name', 'unknown')} with arguments: {item.get('arguments', '')}"
                         for item in response.get("output", [])
                         if item.get("type") == "function_call"),
                        None
                    )
                    if any(item.get("type") == "function_call" for item in response.get("output", []))
                    else (
                        # Try to get content from multiple types with fallback
                        next(
                            (item.get("content", [{}])[0].get("text", None)
                             for item in response.get("output", [])
                             if item.get("type") == "message" and item.get("content", [{}])[0].get("text", None) is not None),
                            None
                        ) or
                        next(
                            (item.get("content", [{}])[0].get("text", None)
                             for item in response.get("output", [])
                             if item.get("type") == "output_text" and item.get("content", [{}])[0].get("text", None) is not None),
                            None
                        ) or
                        next(
                            (item.get("content", [{}])[0].get("text", None)
                             for item in response.get("output", [])
                             if item.get("type") == "reasoning" and item.get("content", [{}])[0].get("text", None) is not None),
                            None
                        )
                    )
                ),
                "model": response.get("model", None),
                "role": 'assistant',
                "finish_reason":  finish_reason_mapping(response.get("status", "")) if response.get("status", None) == "in_progress" or response.get("status", None) == "completed" else finish_reason_mapping(response.get("incomplete_details", {}).get("reason", None)) ,
                "tools_data": tools_data or {},
                "images": images,
                "annotations": response.get("output", [{}])[0].get("content", [{}])[0].get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or ''
            },
            "usage": {
                "input_tokens": response.get("usage", {}).get("input_tokens", None),
                "output_tokens": response.get("usage", {}).get("output_tokens", None),
                "total_tokens": response.get("usage", {}).get("total_tokens", None),
                "cached_tokens": response.get("usage", {}).get("input_tokens_details", {}).get('cached_tokens', None)
            }
        }                    
    elif service == service_name['gemini'] and (type !='image' and type != 'embedding' and type != 'video'):
        candidates = response.get('candidates', [{}])
        content = candidates[0].get('content', {}) if candidates else {}
        parts = content.get('parts', [])
        return {
            "data" : {
                "id" : response.get("response_id", None),
                "content" : parts[0].get('text') if parts else None,
                "model" : response.get("model_version", None),
                "role" : "assistant",
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : None,
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason": finish_reason_mapping(candidates[0].get("finish_reason").value.lower() if candidates and candidates[0].get("finish_reason") else "")
            },
            "usage" : {
                "input_tokens" : response.get("usage_metadata", {}).get("prompt_token_count", None),
                "output_tokens" : response.get("usage_metadata", {}).get("candidates_token_count", None),
                "total_tokens" : response.get("usage_metadata", {}).get("total_token_count", None),
                "thoughts_token": response.get("usage_metadata", {}).get("thoughts_token_count", None),
                "input_token_details": response.get("usage_metadata", {}).get("prompt_tokens_details", None),
                "cached_tokens": response.get("usage_metadata", {}).get("cached_content_token_count", None),
                "cache_tokens_details": response.get("usage_metadata", {}).get("cache_tokens_details", None)
            }
        }
    elif service == service_name['openai'] and type == 'embedding':
         return {
            "data" : {
                "embedding" : response.get('data')[0].get('embedding')
            }
        }
    elif service == service_name['gemini'] and type == 'image':
        data_item = response.get('data', [{}])[0]
        # Handle both Imagen models (urls as list) and Gemini models (url as single value)
        urls = data_item.get('urls')
        url = data_item.get('url')
        text_content = data_item.get('text_content', [])
        
        # If Imagen returned multiple URLs, format like OpenAI's multi-image response
        if urls and len(urls) > 0:
            image_urls = []
            for img_url in urls:
                image_urls.append({
                    "revised_prompt": text_content,
                    "image_url": img_url,
                    "permanent_url": img_url
                })
            
            return {
                "data": {
                    "image_urls": image_urls
                }
            }
        else:
            # Single image from Gemini image model
            return {
                "data" : {
                    "revised_prompt" : text_content,
                    "image_url" : url,
                    "permanent_url" : url,
                }
            }
    elif service == service_name['gemini'] and type == 'video':
        return {
            "data" : {
                "content" : response.get('data')[0].get('text_content'),
                "file_data" : response.get('data')[0].get('file_reference')
            }
        }
    elif service == service_name['openai']:
        image_urls = []
        for image_data in response.get('data', []):
            image_urls.append({
                "revised_prompt": image_data.get('revised_prompt'),
                "image_url": image_data.get('original_url'),
                "permanent_url": image_data.get('url')
            })
        
        return {
            "data": {
                "image_urls": image_urls
            }
        }
    
    elif service == service_name['anthropic']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("content", [{}])[0].get("text", None),
                "model" : response.get("model", None),
                "role" : response.get("role", None),
                "tools_data": tools_data or {},
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("stop_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("input_tokens", None),
                "output_tokens" : response.get("usage", {}).get("output_tokens", None),
                "cache_read_input_tokens" : response.get("usage",{}).get("cache_read_input_tokens",None),
                "cache_creation_input_tokens" : response.get("usage",{}).get("cache_creation_input_tokens",None),

                "total_tokens" : (
                    response.get("usage", {}).get("input_tokens", 0) + 
                    response.get("usage", {}).get("output_tokens", 0)
                )
            }
        }
    elif service == service_name['groq']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "fallback" : response.get('fallback') or False,
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None)
            }
        }
    elif service == service_name['grok']:
        return {
            "data": {
                "id": response.get("id", None),
                "content": response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model": response.get("model", None),
                "role": response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images": images,
                "annotations": response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback": response.get('fallback') or False,
                "firstAttemptError": response.get('firstAttemptError') or '',
                "finish_reason": finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage": {
                "input_tokens": response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens": response.get("usage", {}).get("completion_tokens", None),
                "total_tokens": response.get("usage", {}).get("total_tokens", None)
            }
        }
    elif service == service_name['open_router']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None),
                "cached_tokens": (
                    response.get("usage", {})
                    .get("prompt_tokens_details", {}) or {}
                ).get('cached_tokens')

            }
        }
    elif service == service_name['openai_completion']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None),
                "cached_tokens": (
                    response.get("usage", {})
                    .get("prompt_tokens_details", {}) or {}
                ).get('cached_tokens')

            }
        }
    elif service == service_name['mistral']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None),
                "cached_tokens": (
                    response.get("usage", {})
                    .get("prompt_tokens_details", {}) or {}
                ).get('cached_tokens')

            }
        }
    elif service == service_name['ai_ml'] and type == 'image':
        image_urls = []
        for image_data in response.get('data', []):
            image_urls.append({
                "revised_prompt": image_data.get('revised_prompt'),
                "image_url": image_data.get('original_url'),
                "permanent_url": image_data.get('url'),
                "size": image_data.get('size')
            })
        
        return {
            "data": {
                "image_urls": image_urls
            },
            "usage": {
                "generated_images": response.get("usage", {}).get("generated_images", None),
                "output_tokens": response.get("usage", {}).get("output_tokens", None),
                "total_tokens": response.get("usage", {}).get("total_tokens", None)
            }
        }
    elif service == service_name['ai_ml']:
        return {
            "data" : {
                "id" : response.get("id", None),
                "content" : response.get("choices", [{}])[0].get("message", {}).get("content", None),
                "model" : response.get("model", None),
                "role" : response.get("choices", [{}])[0].get("message", {}).get("role", None),
                "tools_data": tools_data or {},
                "images" : images,
                "annotations" : response.get("choices", [{}])[0].get("message", {}).get("annotations", None),
                "fallback" : response.get('fallback') or False,
                "firstAttemptError" : response.get('firstAttemptError') or '',
                "finish_reason" : finish_reason_mapping(response.get("choices", [{}])[0].get("finish_reason", ""))
            },
            "usage" : {
                "input_tokens" : response.get("usage", {}).get("prompt_tokens", None),
                "output_tokens" : response.get("usage", {}).get("completion_tokens", None),
                "total_tokens" : response.get("usage", {}).get("total_tokens", None),
                "cached_tokens" : response.get("usage", {}).get("prompt_tokens_details",{}).get('cached_tokens')

            }
        }

async def validateResponse(alert_flag, configration, bridgeId, message_id, org_id):
    if alert_flag:
        await send_alert(data={"response":"\n..\n","configration":configration,"message_id":message_id,"bridge_id":bridgeId, "org_id": org_id, "message": "\n issue occurs"})

async def send_alert(data):
    dataTosend = {**data, "ENVIROMENT":Config.ENVIROMENT} if Config.ENVIROMENT else data
    await fetch("https://flow.sokt.io/func/scriYP8m551q",method='POST',json_body=dataTosend)

def finish_reason_mapping(finish_reason):
    finish_reason_mapping = {
        # Completed / natural stop
        "stop": "completed",        #openai #open_router #gemini
        "end_turn": "completed",    #anthropic
        "completed": "completed",   #openai_response

        # Truncation due to token limits
        "length": "truncated",              #openai #open_router #gemini
        "max_tokens": "truncated",          #anthropic
        "max_output_tokens": "truncated",    #openai_response

        # Tool / function invocation
        "tool_calls": "tool_call",    #openai #gemini
        "tool_use": "tool_call",      #anthropic
    }
    return finish_reason_mapping.get(finish_reason, "other")

async def Batch_Response_formatter(response = {}, service = None, tools={}, type='chat', images = None, batch_id = None, custom_id = None, isBatch = True):
    """
    Formatter specifically for batch responses that includes batch_id and custom_id for easy mapping
    
    Args:
        isBatch: Boolean flag to indicate this is a batch response (default: True)
    """
    # Get the base formatted response with isBatch flag
    formatted_response = await Response_formatter(response=response, service=service, tools=tools, type=type, images=images, isBatch=isBatch)
    print(formatted_response)
    # Add batch_id and custom_id to the response for mapping
    formatted_response["batch_id"] = batch_id
    formatted_response["custom_id"] = custom_id
    formatted_response["isBatch"] = isBatch
    
    return formatted_response

async def process_batch_results(results, service, batch_id, batch_variables, custom_id_mapping):
    """
    Common function to process batch results for all services.
    
    Args:
        results: List of result items
        service: Service name (e.g., 'gemini', 'anthropic')
        batch_id: Batch ID
        batch_variables: Optional batch variables
        custom_id_mapping: Mapping of custom_id to index
        
    Returns:
        List of formatted results
    """
    formatted_results = []
    
    for index, result_item in enumerate(results):
        # Check if this is a pre-formatted error from terminal batch failure (failed, expired, cancelled)
        # These come directly from the batch handler and are already formatted
        if "error" in result_item and "status_code" in result_item and "custom_id" not in result_item:
            # This is a terminal batch error (not an individual request error)
            # Pass it through as-is, just add batch_id
            result_item["batch_id"] = batch_id
            formatted_results.append(result_item)
            continue
        
        # Extract custom_id and result data (format varies by service)
        if service == 'gemini':
            custom_id = result_item.get("key", None)
            result_data = result_item.get("response", {})
            result_data = custom_id
        elif service == 'anthropic':
            custom_id = result_item.get("custom_id", None)
            result_data = result_item.get("result", {})
            if result_data.get("type") != "error":
                result_data = result_data.get("message", {})
        elif service in ['openai', 'groq']:
            custom_id = result_item.get("custom_id", None)
            response = result_item.get("response", {})
            result_data = response.get("body", {})
            status_code = response.get("status_code", 200)
        elif service == 'mistral':
            custom_id = result_item.get("custom_id", None)
            result_data = result_item.get("response", {})
        
        # Check for errors
        has_error = False
        if service in {'openai', 'groq'}:
            has_error = status_code >= 400 or "error" in result_data
        elif service == 'anthropic':
            has_error = result_data.get("type") == "error"
        else:
            has_error = result_data.get("error")
        
        if has_error:
            formatted_content = {
                "custom_id": custom_id,
                "batch_id": batch_id,
                "error": result_data.get("error", result_data),
                "status_code": status_code if service in ['openai', 'groq'] else 400
            }
        else:
            # Format successful response
            formatted_content = await Batch_Response_formatter(
                response=result_data,
                service=service,
                tools={},
                type='chat',
                images=None,
                batch_id=batch_id,
                custom_id=custom_id,
                isBatch=True
            )
        
        # Add batch_variables to response if available
        if batch_variables is not None and custom_id in custom_id_mapping:
            variable_index = custom_id_mapping[custom_id]
            if variable_index < len(batch_variables):
                formatted_content["variables"] = batch_variables[variable_index]
        
        formatted_results.append(formatted_content)
    
    return formatted_results
