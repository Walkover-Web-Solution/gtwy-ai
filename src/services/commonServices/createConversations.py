import traceback
from ..utils.apiservice import fetch_images_b64
from globals import *

class ConversationService:
    @staticmethod
    def createOpenAiConversation(conversation, memory, files):
        try:
            threads = []
            # Track distinct PDF URLs across the entire conversation
            seen_pdf_urls = set()
            
            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}' })
            for message in conversation or []:
                if message['role'] != "tools_call" and message['role'] != "tool":
                    if message['role'] == "assistant":
                        content = [{"type": "output_text", "text": message['content']}]
                    else:
                        content = [{"type": "input_text", "text": message['content']}]
                    
                    if 'user_urls' in message and isinstance(message['user_urls'], list):
                        for url in message['user_urls']:
                            if url.get('type') == 'image':
                                content.append({
                                    "type": "input_image",
                                    "image_url": url.get('url')
                                })
                            elif url.get('url') not in files and url.get('url') not in seen_pdf_urls:
                                content.append({
                                    "type": "input_file",
                                    "file_url": url.get('url')
                                })
                                # Add to seen URLs to prevent duplicates
                                seen_pdf_urls.add(url.get('url'))
                    else:
                        # Default behavior for messages without URLs
                        content = message['content']
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            print("create conversation error=>", e)
            raise ValueError(e.args[0])

    @staticmethod
    async def createAnthropicConversation(conversation, memory, files):
        try:
            if conversation == None:
                conversation = []
            threads = []
            # Track distinct PDF URLs across the entire conversation
            seen_pdf_urls = set()
            
            if memory is not None:
                threads.append({'role': 'user', 'content': [{"type": "text", "text": f"GPT-Memory Data:- {memory}"}]})
                threads.append({'role': 'assistant', 'content': [{"type": "text", "text": "memory updated."}]})
            
            # Process image URLs if present
            image_urls = [url.get('url') for message in conversation for url in message.get('user_urls', [])]
            images_data = await fetch_images_b64(image_urls) if image_urls else []
            images = {url: data for url, data in zip(image_urls, images_data)}
            
            valid_conversation = []
            expected_role = 'user'
            
            for message in conversation:
                if message['role'] not in ['assistant', 'user']:
                    continue  # Skip invalid roles
                
                # Skip messages with empty content
                if not message.get('content') or message['content'].strip() == '':
                    continue
                
                # If role doesn't match expected, skip this message
                if message['role'] != expected_role:
                    continue
                
                valid_conversation.append(message)
                expected_role = 'user' if expected_role == 'assistant' else 'assistant'
            for message in valid_conversation:
                content_items = []
                
                # Handle image URLs
                if message.get('user_urls'):
                    image_data = []
                    for url_info in message['user_urls']:
                        url = url_info.get("url")
                        if url in images:
                            img_type = url_info.get("type")
                            if img_type == 'image':
                                image_data.append({
                                    'type': 'image',
                                    'source': {
                                        'type': 'base64',
                                        'media_type': images[url][1],
                                        'data': images[url][0]
                                    }
                                })
                            elif img_type == 'pdf':
                                image_data.append({
                                    'type': 'document',
                                    'source': {
                                        'type': 'url',
                                        'url': url
                                    }
                                })
                            else:
                                image_data.append({
                                    'type': 'image',
                                    'source': {
                                        'type': 'url',
                                        'url': url
                                    }
                                })


                    content_items.extend(image_data)
                
                # Add text content
                content_items.append({"type": "text", "text": message['content']})
                
                threads.append({
                    'role': message['role'],
                    'content': content_items
                })
            return {
                'success': True,
                'messages': threads
            }
        except Exception as e:
            logger.error(f"create conversation error=>, {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'messages': []
            }

    def createGroqConversation(conversation, memory):
        try:
            threads = []
            
            # If memory is provided, add it as the first message
            if memory is not None:
                threads.append({'role': 'user', 'content': memory})
            
            # Loop through the conversation to build the message threads
            for message in conversation or []:
                if message['role'] not in ["tools_call", "tool"]:  # Skip tool-related roles
                    # Ensure content is a string (no image URLs, handle properly)
                    content = message['content']
                    
                    # If the role is 'assistant', ensure the content is a plain string
                    if message['role'] == 'assistant':
                        threads.append({'role': message['role'], 'content': content})
                    else:
                        # For other roles, wrap the content as 'text'
                        threads.append({'role': message['role'], 'content': [{"type": "text", "text": content}]})
            
            # Return the constructed messages in the required format for Groq
            return {
                'success': True,
                'messages': threads
            }
        
        except Exception as e:
            logger.error(f"create conversation error=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"Error while creating conversation: {str(e)}")

    @staticmethod
    def createGrokConversation(conversation, memory, files=None, image_urls=None):
        try:
            threads = []

            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}'})

            for message in conversation or []:
                if message['role'] in ["tools_call", "tool"]:
                    continue

                content = [{"type": "text", "text": message['content']}]

                if 'urls' in message and isinstance(message['urls'], list):
                    for url in message['urls']:
                        if not url.lower().endswith('.pdf'):
                            content.append({
                                "type": "image_url",
                                "image_url": {"url": url}
                            })

                threads.append({'role': message['role'], 'content': content})

            return {
                'success': True,
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0])

    @staticmethod
    def createOpenRouterConversation(conversation, memory):
        try:
            threads = []
            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}' })
            for message in conversation or []:
                if message['role'] != "tools_call" and message['role'] != "tool":
                    content = [{"type": "text", "text": message['content']}]
                    if 'urls' in message and isinstance(message['urls'], list):
                        for url in message['urls']:
                            if not url.lower().endswith('.pdf'):
                                content.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": url
                                    }
                                })
                    else:
                        # Default behavior for messages without URLs
                        content = message['content']
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0])

    @staticmethod
    def create_mistral_ai_conversation(conversation, memory):
        try:
            threads = []
            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}' })
            for message in conversation or []:
                if message['role'] != "tools_call" and message['role'] != "tool":
                    content = [{"type": "text", "text": message['content']}]
                    if 'urls' in message and isinstance(message['urls'], list):
                        for url in message['urls']:
                            if not url.lower().endswith('.pdf'):
                                content.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": url
                                    }
                                })
                    else:
                        # Default behavior for messages without URLs
                        content = message['content']
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0])

    @staticmethod
    def createGeminiConversation(conversation, memory):
        from google.genai import types
        try:
            contents = []
            if memory is not None:
                contents.append(types.Content(role='user', parts=[types.Part(text='Please Provide the summary of the previous conversation stored in the memory.')]))
                contents.append(types.Content(role='model', parts=[types.Part(text=f'Summary of previous conversations: {memory}')]))
            
            for message in conversation or []:
                role = message.get('role')
                if role not in {'tools_calls', "tools"}:
                    gemini_role = 'model' if role == 'assistant' else 'user'
                    parts = []

                    msg_content = message.get('content')
                    if msg_content:
                        parts.append(types.Part(text=msg_content))
                    
                    if 'urls' in message and isinstance(message['urls'], list):
                        for url in message['urls']:
                            if url.lower().endswith('.pdf'):
                                parts.append(types.Part.from_url(file_uri=url, mime_type="application/pdf"))
                            else:
                                mime_type = "image/png" if url.lower().endswith(".png") else "image/jpeg"
                                parts.append(types.Part.from_url(file_uri=url, mime_type=(mime_type)))
                    if parts:
                        contents.append(types.Content(role=gemini_role, parts=parts))
            
            return {
                "success": True,
                "messages": contents
            }
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0])
    @staticmethod
    def createOpenaiCompletionConversation(conversation, memory):
        try:
            threads = []
            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}' })
            for message in conversation or []:
                if message['role'] != "tools_call" and message['role'] != "tool":
                    content = [{"type": "text", "text": message['content']}]
                    if 'urls' in message and isinstance(message['urls'], list):
                        for url in message['urls']:
                            if not url.lower().endswith('.pdf'):
                                content.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": url
                                    }
                                })
                    else:
                        # Default behavior for messages without URLs
                        content = message['content']
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0])
    
    @staticmethod
    def createAiMlConversation(conversation, memory, files):
        try:
            threads = []
            # Track distinct PDF URLs across the entire conversation
            seen_pdf_urls = set()
            
            if memory is not None:
                threads.append({'role': 'user', 'content': 'provide the summary of the previous conversation stored in the memory?'})
                threads.append({'role': 'assistant', 'content': f'Summary of previous conversations :  {memory}' })
            for message in conversation or []:
                if message['role'] != "tools_call" and message['role'] != "tool":
                    content = [{"type": "text", "text": message['content']}]
                    
                    if 'user_urls' in message and isinstance(message['user_urls'], list):
                        for url in message['user_urls']:
                            if url.get('type') == 'image':
                                content.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url":url.get('url')
                                    }
                                })
                            elif url.get('url') not in files and url.get('url') not in seen_pdf_urls:
                                content.append({
                                    "type": "input_file",
                                    "file_url": {
                                        "url":url.get('url')
                                    }
                                })
                                # Add to seen URLs to prevent duplicates
                                seen_pdf_urls.add(url.get('url'))
                    else:
                        # Default behavior for messages without URLs
                        content = message['content']
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            print("create conversation error=>", e)
            raise ValueError(e.args[0])