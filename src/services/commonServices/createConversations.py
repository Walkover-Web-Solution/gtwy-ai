import json
import mimetypes
import traceback
from urllib.parse import urlparse

from globals import logger

from ..utils.apiservice import fetch_images_b64


def _format_memory(memory):
    if isinstance(memory, dict):
        return json.dumps(memory, ensure_ascii=False)
    return memory


class ConversationService:
    @staticmethod
    def createOpenAiConversation(conversation, memory, files):
        try:
            threads = []
            # Track distinct PDF URLs across the entire conversation
            seen_pdf_urls = set()

            if memory is not None:
                threads.append(
                    {
                        "role": "user",
                        "content": "provide the summary of the previous conversation stored in the memory?",
                    }
                )
                threads.append({"role": "assistant", "content": f"Summary of previous conversations :  {_format_memory(memory)}"})
            for message in conversation or []:
                if message['role'] not in ["tools_call", "tool"]:
                    has_media = 'user_urls' in message and isinstance(message['user_urls'], list) and len(message['user_urls']) > 0
                    content_text = message.get('content') or ""
                    
                    if not content_text.strip() and not has_media:
                        continue
                    
                    if message['role'] == "assistant":
                        content = [{"type": "output_text", "text": content_text if content_text else " "}]
                    else:
                        if has_media:
                            content = []
                            if content_text.strip():
                                content.append({"type": "input_text", "text": content_text})
                            
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
                                    seen_pdf_urls.add(url.get('url'))
                        else:
                            content = content_text if content_text else " "
                    
                    threads.append({'role': message['role'], 'content': content})
            
            return {
                'success': True, 
                'messages': threads
            }
        except Exception as e:
            traceback.print_exc()
            print("create conversation error=>", e)
            raise ValueError(e.args[0]) from e

    @staticmethod
    async def createAnthropicConversation(conversation, memory, files):
        try:
            if conversation is None:
                conversation = []
            threads = []
            # Track distinct PDF URLs across the entire conversation

            if memory is not None:
                threads.append({"role": "user", "content": [{"type": "text", "text": f"GPT-Memory Data:- {_format_memory(memory)}"}]})
                threads.append({"role": "assistant", "content": [{"type": "text", "text": "memory updated."}]})

            # Process image URLs if present
            image_urls = [url.get("url") for message in conversation for url in message.get("user_urls", [])]
            images_data = await fetch_images_b64(image_urls) if image_urls else []
            images = dict(zip(image_urls, images_data, strict=False))

            valid_conversation = []
            expected_role = "user"

            for message in conversation:
                if message["role"] not in ["assistant", "user"]:
                    continue  # Skip invalid roles

                # Skip messages with empty content
                if not message.get("content") or message["content"].strip() == "":
                    continue

                # If role doesn't match expected, skip this message
                if message["role"] != expected_role:
                    continue

                valid_conversation.append(message)
                expected_role = "user" if expected_role == "assistant" else "assistant"
            for message in valid_conversation:
                content_items = []

                # Handle image URLs
                if message.get("user_urls"):
                    image_data = []
                    for url_info in message["user_urls"]:
                        url = url_info.get("url")
                        if url in images:
                            img_type = url_info.get("type")
                            if img_type == "image":
                                image_data.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": images[url][1],
                                            "data": images[url][0],
                                        },
                                    }
                                )
                            elif img_type == "pdf":
                                image_data.append({"type": "document", "source": {"type": "url", "url": url}})
                            else:
                                image_data.append({"type": "image", "source": {"type": "url", "url": url}})

                    content_items.extend(image_data)

                # Add text content
                content_items.append({"type": "text", "text": message["content"]})

                threads.append({"role": message["role"], "content": content_items})
            return {"success": True, "messages": threads}
        except Exception as e:
            logger.error(f"create conversation error=>, {str(e)}")
            return {"success": False, "error": str(e), "messages": []}

    def createGroqConversation(conversation, memory):
        try:
            threads = []

            # If memory is provided, add it as the first message
            if memory is not None:
                threads.append({"role": "user", "content": _format_memory(memory)})

            # Loop through the conversation to build the message threads
            for message in conversation or []:
                if message["role"] not in ["tools_call", "tool"]:  # Skip tool-related roles
                    # Ensure content is a string (no image URLs, handle properly)
                    content = message["content"]

                    # If the role is 'assistant', ensure the content is a plain string
                    if message["role"] == "assistant":
                        threads.append({"role": message["role"], "content": content})
                    else:
                        # For other roles, wrap the content as 'text'
                        threads.append({"role": message["role"], "content": [{"type": "text", "text": content}]})

            # Return the constructed messages in the required format for Groq
            return {"success": True, "messages": threads}

        except Exception as e:
            logger.error(f"create conversation error=>, {str(e)}, {traceback.format_exc()}")
            raise ValueError(f"Error while creating conversation: {str(e)}") from e

    @staticmethod
    def createOpenAICompatibleConversation(conversation, memory, files=None, image_urls=None, plain_text_fallback=True):
        """Shared conversation builder for OpenAI-Chat-compatible services
        (open_router, neev_cloud, moonshot, mistral, openai_completion, grok, deepseek).

        plain_text_fallback=True  -> a message with no URLs uses the raw string as
            content (open_router / mistral / openai_completion behavior).
        plain_text_fallback=False -> content always stays a [{type:text}] list
            (grok / deepseek behavior).

        ``files`` / ``image_urls`` are accepted for caller compatibility (unused).
        """
        try:
            threads = []
            if memory is not None:
                threads.append(
                    {
                        "role": "user",
                        "content": "provide the summary of the previous conversation stored in the memory?",
                    }
                )
                threads.append({"role": "assistant", "content": f"Summary of previous conversations :  {_format_memory(memory)}"})
            for message in conversation or []:
                if message["role"] != "tools_call" and message["role"] != "tool":
                    content = [{"type": "text", "text": message["content"]}]
                    if "urls" in message and isinstance(message["urls"], list):
                        for url in message["urls"]:
                            if not url.lower().endswith(".pdf"):
                                content.append({"type": "image_url", "image_url": {"url": url}})
                    elif plain_text_fallback:
                        # Default behavior for messages without URLs
                        content = message["content"]
                    threads.append({"role": message["role"], "content": content})

            return {"success": True, "messages": threads}
        except Exception as e:
            traceback.print_exc()
            logger.error(f"create conversation error=>, {str(e)}")
            raise ValueError(e.args[0]) from e

    @staticmethod
    def createGeminiConversation(conversation, memory):
        from google.genai import types
        try:
            contents = []
            if memory is not None:
                contents.append(types.Content(role='user', parts=[types.Part(text='Please Provide the summary of the previous conversation stored in the memory.')]))
                contents.append(types.Content(role='model', parts=[types.Part(text=f'Summary of previous conversations: {_format_memory(memory)}')]))
            
            for message in conversation or []:
                role = message.get('role')
                if role not in {'tools_calls', "tools"}:
                    gemini_role = 'model' if role == 'assistant' else 'user'
                    parts = []

                    msg_content = message.get('content')
                    if msg_content:
                        parts.append(types.Part(text=msg_content))
                    
                    if 'user_urls' in message and isinstance(message['user_urls'], list):
                        for url_info in message['user_urls']:
                            url = url_info.get('url')
                            url_type = url_info.get('type')
                            if url_type == 'pdf' or url.lower().endswith('.pdf'):
                                parts.append(types.Part.from_uri(file_uri=url, mime_type="application/pdf"))
                            elif url_type == 'audio':
                                mime_type, _ = mimetypes.guess_type(urlparse(url).path)
                                parts.append(types.Part.from_uri(file_uri=url, mime_type=mime_type))
                            else:
                                mime_type, _ = mimetypes.guess_type(urlparse(url).path)
                                parts.append(types.Part.from_uri(file_uri=url, mime_type=mime_type))
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

