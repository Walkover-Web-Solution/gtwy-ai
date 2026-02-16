import json
import uuid

from src.services.commonServices.baseService.baseService import BaseService


def add_prompt_and_conversations(custom_config, conversations, service, prompt):
    custom_config["messages"] = custom_messages(
        custom_config, make_conversations_as_per_service(conversations, service), service, prompt
    )
    base_service = BaseService({})
    return base_service.service_formatter(custom_config, service)


def make_conversations_as_per_service(conversations, service):
    Newconversations = []
    for conversation in conversations:
        match service:
            case "openai" | "groq":
                if conversation.get("role") == "tools_call":
                    id = f"call_{uuid.uuid4().hex[:6]}"
                    for i, tools in enumerate(conversation.get("content")):
                        convers = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"{id}{i}{j}",
                                    "type": "function",
                                    "function": {"name": tool["name"], "arguments": json.dumps(tool.get("args", {}))},
                                }
                                for j, tool in enumerate(tools)
                            ],
                        }
                        Newconversations.append(convers)
                        for j, tool in enumerate(tools):
                            conversResponse = {
                                "role": "tool",
                                "content": json.dumps(
                                    {
                                        "response": tool["response"],
                                        "metadata": tool["metadata"],
                                        "status": tool["status"],
                                    }
                                ),
                                "tool_call_id": f"{id}{i}{j}",
                            }
                            Newconversations.append(conversResponse)
                else:
                    Newconversations.append(conversation)
            case "anthropic":
                if conversation.get("role") == "tools_call":
                    id = f"toolu_{uuid.uuid4().hex[:6]}"
                    for i, tools in enumerate(conversation.get("content")):
                        convers = {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "Call the function for better response"},
                                *[
                                    {
                                        "id": f"{id}{i}{j}",
                                        "type": "tool_use",
                                        "name": tool["name"],
                                        "input": tool.get("args", {}),
                                    }
                                    for j, tool in enumerate(tools)
                                ],
                            ],
                        }
                        Newconversations.append(convers)
                        conversResponse = {
                            "role": "user",
                            "content": [
                                {
                                    "tool_use_id": f"{id}{i}{j}",
                                    "type": "tool_result",
                                    "content": json.dumps(
                                        {
                                            "response": tool["response"],
                                            "metadata": tool["metadata"],
                                            "status": tool["status"],
                                        }
                                    ),
                                }
                                for j, tool in enumerate(tools)
                            ],
                        }
                        Newconversations.append(conversResponse)
                else:
                    Newconversations.append(conversation)

    return Newconversations


def custom_messages(custom_config, conversations, service, prompt):
    messages = []
    match service:
        case "openai":
            messages = [{"role": "developer", "content": prompt}] + conversations
        case "anthropic":
            custom_config["system"] = prompt
            messages = conversations
        case "groq":
            messages = [{"role": "system", "content": prompt}] + conversations
    return messages
