from anthropic import AsyncAnthropic
import traceback
import json
from ..api_executor import execute_api_call
# from src.services.utils.unified_token_validator import validate_anthropic_token_limit
from globals import *


async def anthropic_runmodel(configuration, apikey, execution_time_logs, bridge_id, timer, name = "", org_name = "", service = "", count = 0, token_calculator=None):
    try:
        # # Validate token count before making API call
        # model_name = configuration.get('model')
        # validate_anthropic_token_limit(configuration, model_name, service, apikey)
        
        # Initialize async client
        anthropic_client = AsyncAnthropic(api_key=apikey)

        # Define the API call function with streaming
        async def api_call(config):
            try:
                # Initialize response structure to accumulate streaming data
                accumulated_response = {
                    'id': '',
                    'type': 'message',
                    'role': 'assistant',
                    'content': [],
                    'model': config.get('model', ''),
                    'stop_reason': None,
                    'stop_sequence': None,
                    'usage': {'input_tokens': 0, 'output_tokens': 0}
                }
                
                # Track content blocks
                content_blocks = {}
                
                # Create streaming response (stream method doesn't need 'stream' parameter)
                async with anthropic_client.messages.stream(**config) as stream:
                    async for event in stream:
                        if event.type == 'message_start':
                            # Initialize message with basic info
                            message_data = event.message
                            accumulated_response['id'] = message_data.id
                            accumulated_response['model'] = message_data.model
                            accumulated_response['usage']['input_tokens'] = message_data.usage.input_tokens
                            
                        elif event.type == 'content_block_start':
                            # Initialize content block
                            index = event.index
                            content_block = event.content_block
                            
                            if content_block.type == 'text':
                                content_blocks[index] = {
                                    'type': 'text',
                                    'text': ''
                                }
                            elif content_block.type == 'tool_use':
                                initial_input = getattr(content_block, 'input', None)
                                if initial_input is None:
                                    initial_input = {}
                                content_blocks[index] = {
                                    'type': 'tool_use',
                                    'id': content_block.id,
                                    'name': content_block.name,
                                    'input': initial_input
                                }
                            elif content_block.type == 'thinking':
                                content_blocks[index] = {
                                    'type': 'thinking',
                                    'thinking': ''
                                }
                                
                        elif event.type == 'content_block_delta':
                            # Accumulate content
                            index = event.index
                            delta = event.delta

                            block = content_blocks.get(index)
                            if not block:
                                continue

                            if delta.type == 'text_delta':
                                block.setdefault('text', '')
                                block['text'] += delta.text
                            elif delta.type == 'input_json_delta' and block.get('type') == 'tool_use':
                                # For tool use, we need to accumulate the JSON string
                                block.setdefault('partial_json', '')
                                block['partial_json'] += delta.partial_json
                            elif delta.type == 'input_text_delta' and block.get('type') == 'tool_use':
                                block.setdefault('partial_text', '')
                                block['partial_text'] += delta.partial_text
                            elif delta.type == 'thinking_delta':
                                block.setdefault('thinking', '')
                                block['thinking'] += delta.thinking
                                
                        elif event.type == 'content_block_stop':
                            # Finalize content block
                            index = event.index
                            if index in content_blocks:
                                block = content_blocks[index]
                                if block['type'] == 'tool_use':
                                    if 'partial_json' in block:
                                        # Parse the accumulated JSON for tool input
                                        try:
                                            if block['partial_json'] == "":
                                                block['input'] = {}
                                            else:
                                                block['input'] = json.loads(block['partial_json'])
                                        except json.JSONDecodeError:
                                            # If JSON parsing fails, keep as string
                                            block['input'] = block.get('partial_json', {})
                                        finally:
                                            del block['partial_json']  # Remove temporary field
                                    if 'partial_text' in block:
                                        existing_input = block.get('input')
                                        partial_text = block.pop('partial_text')
                                        if isinstance(existing_input, str):
                                            block['input'] = existing_input + partial_text
                                        elif existing_input in (None, {}, []):
                                            block['input'] = partial_text
                                        else:
                                            block['input'] = partial_text
                                        
                        elif event.type == 'message_delta':
                            # Update message-level information
                            delta = event.delta
                            if hasattr(delta, 'stop_reason') and delta.stop_reason:
                                accumulated_response['stop_reason'] = delta.stop_reason
                            if hasattr(delta, 'stop_sequence') and delta.stop_sequence:
                                accumulated_response['stop_sequence'] = delta.stop_sequence
                            # Update usage if present
                            if hasattr(event, 'usage') and event.usage:
                                accumulated_response['usage']['output_tokens'] = event.usage.output_tokens
                                
                        elif event.type == 'message_stop':
                            # Finalize the response
                            break
                
                # Convert content_blocks dict to ordered list
                ordered_content = [content_blocks[i] for i in sorted(content_blocks.keys())]

                merged_content = []
                current_text_block = None

                for block in ordered_content:
                    if block.get('type') == 'text':
                        if current_text_block is None:
                            current_text_block = {
                                'type': 'text',
                                'text': block.get('text', '')
                            }
                        else:
                            current_text_block['text'] += block.get('text', '')
                    else:
                        if current_text_block is not None:
                            merged_content.append(current_text_block)
                            current_text_block = None
                        merged_content.append(block)

                if current_text_block is not None:
                    merged_content.append(current_text_block)

                accumulated_response['content'] = merged_content
                return {'success': True, 'response': accumulated_response}
                
            except Exception as error:
                return {
                    'success': False,
                    'error': str(error),
                    'status_code': getattr(error, 'status_code', None)
                }

        # Execute API call with monitoring
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=None,  # Adjust if needed
            org_id=None,      # Adjust if needed
            alert_on_retry=False,  # Adjust if needed
            name=name,
            org_name=org_name,
            service = service,
            count = count,
            token_calculator = token_calculator
        )

    except Exception as e:
        execution_time_logs.append({"step": f"{service} Processing time for call :- {count + 1}", "time_taken": timer.stop("API chat completion")})
        logger.error("Anthropic runmodel error=>", e)
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }
