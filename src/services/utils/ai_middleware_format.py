import json
import uuid

from src.configs.constant import service_name
from src.configs.service_registry import has_openai_choices_shape
from src.services.utils.batch_script_utils import get_batch_result_data

# Per-service response formatters. Each service owns all of its response variants
# (chat / batch / image / video / embedding) in its own file; the plain
# OpenAI-chat services share one formatter.
from src.services.utils.formatters.anthropic_formatter import format_anthropic
from src.services.utils.formatters.finish_reason import finish_reason_mapping  # re-exported for backward compat
from src.services.utils.formatters.deepgram_formatter import format_deepgram
from src.services.utils.formatters.deepseek_formatter import format_deepseek
from src.services.utils.formatters.gemini_formatter import format_gemini
from src.services.utils.formatters.grok_formatter import format_grok
from src.services.utils.formatters.groq_formatter import format_groq
from src.services.utils.formatters.openai_compatible_formatter import format_openai_compatible
from src.services.utils.formatters.openai_formatter import format_openai

__all__ = ["Response_formatter", "Batch_Response_formatter", "process_batch_results", "finish_reason_mapping"]


async def Response_formatter(response=None, service=None, tools=None, type="chat", images=None, isBatch=False, isCache=False):
    """Normalize a provider response into the AI-middleware format.

    Thin dispatcher: routes by service to the per-service formatter, each of
    which handles that service's own variants (chat / batch / image / video /
    embedding). See src/services/utils/formatters/.
    """
    if isCache:
        return {
            "data": {
                "id": f"cache_{uuid.uuid4().hex}",
                "content": response,
                "model": None,
                "role": "assistant",
                "tools_data": {},
                "images": None,
                "annotations": None,
                "fallback": False,
                "firstAttemptError": "",
            },
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost": 0,
            },
            "is_cached": True,
        }

    tools_data = tools
    if isinstance(tools_data, dict):
        for key, value in tools_data.items():
            if isinstance(value, str):
                try:
                    tools_data[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass

    if service == service_name["gemini"]:
        return format_gemini(response, tools_data, images, type, isBatch)
    elif service == service_name["anthropic"]:
        return format_anthropic(response, tools_data, images, isBatch)
    elif service == service_name["openai"]:
        return format_openai(response, tools_data, images, type)
    elif service == service_name["groq"]:
        return format_groq(response, tools_data, images)
    elif service == service_name["deepseek"]:
        return format_deepseek(response, tools_data, images)
    elif service == service_name["grok"]:
        return format_grok(response, tools_data, images)
    elif has_openai_choices_shape(service):
        # open_router / neev_cloud / moonshot / openai_completion / mistral (+ future)
        return format_openai_compatible(response, tools_data, images)
    elif service == service_name["deepgram"]:
        return format_deepgram(response, tools_data, images)


async def Batch_Response_formatter(
    response=None, service=None, tools=None, type="chat", images=None, batch_id=None, message_id=None, isBatch=True
):
    """
    Formatter specifically for batch responses that includes batch_id and message_id for easy mapping

    Args:
        isBatch: Boolean flag to indicate this is a batch response (default: True)
    """
    # Get the base formatted response with isBatch flag
    formatted_response = await Response_formatter(
        response=response, service=service, tools=tools, type=type, images=images, isBatch=isBatch
    )
    # Add batch_id and message_id to the response for mapping
    formatted_response["batch_id"] = batch_id
    formatted_response["message_id"] = message_id
    formatted_response["isBatch"] = isBatch

    return formatted_response


async def process_batch_results(results, service, batch_id, batch_variables, message_id_mapping):
    """
    Common function to process batch results for all services.

    Args:
        results: List of result items
        service: Service name (e.g., 'gemini', 'anthropic')
        batch_id: Batch ID
        batch_variables: Optional batch variables
        message_id_mapping: Mapping of message_id to index

    Returns:
        List of formatted results
    """
    formatted_results = []

    for _index, result_item in enumerate(results):
        # Check if this is a pre-formatted error from terminal batch failure (failed, expired, cancelled)
        # These come directly from the batch handler and are already formatted
        if "error" in result_item and "status_code" in result_item and "custom_id" not in result_item:
            # This is a terminal batch error (not an individual request error)
            # Pass it through as-is, just add batch_id
            result_item["batch_id"] = batch_id
            formatted_results.append(result_item)
            continue

        # Extract message_id from result (sent as custom_id/key to the APIs)
        # The external API returns our message_id in their custom_id/key field
        message_id, result_data, status_code, has_error = get_batch_result_data(result_item, service)

        if has_error:
            formatted_content = {
                "message_id": message_id,
                "batch_id": batch_id,
                "error": result_data.get("error", result_data),
                "status_code": status_code if service in ["openai", "groq"] else 400,
            }
        else:
            # Format successful response
            formatted_content = await Batch_Response_formatter(
                response=result_data,
                service=service,
                tools={},
                type="chat",
                images=None,
                batch_id=batch_id,
                message_id=message_id,
                isBatch=True,
            )

        # Add batch_variables to response if available
        if batch_variables is not None and message_id in message_id_mapping:
            variable_index = message_id_mapping[message_id]
            if variable_index < len(batch_variables):
                formatted_content["variables"] = batch_variables[variable_index]

        formatted_results.append(formatted_content)

    return formatted_results
