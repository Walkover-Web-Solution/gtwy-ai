"""Response formatter for the Gemini service.

Handles all Gemini response variants: batch, chat (incl. reasoning/thought
parts), image generation, and video. Returns None for unsupported variants
(e.g. embedding), matching the original fall-through behavior.
"""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_gemini(response, tools_data, images, type="chat", isBatch=False):
    if isBatch:
        return _format_batch(response, tools_data, images)
    if type == "image":
        return _format_image(response)
    if type == "video":
        return _format_video(response)
    if type == "embedding":
        return None  # no gemini embedding formatter (preserves original fall-through)
    return _format_chat(response, tools_data, images)


def _format_batch(response, tools_data, images):
    # Gemini batch responses have a different structure
    candidates = response.get("candidates", [{}])
    content_parts = candidates[0].get("content", {}).get("parts", [{}]) if candidates else [{}]
    return {
        "data": {
            "id": response.get("responseId", None),  # Use the key from batch response as ID
            "content": content_parts[0].get("text", None) if content_parts else None,
            "model": response.get("modelVersion", None),
            "role": candidates[0].get("content", {}).get("role", "model") if candidates else "model",
            "tools_data": tools_data or {},
            "images": images,
            "annotations": None,
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping(candidates[0].get("finishReason", "").lower() if candidates else ""),
        },
        "usage": {
            "input_tokens": response.get("usageMetadata", {}).get("promptTokenCount", 0),
            "output_tokens": response.get("usageMetadata", {}).get("candidatesTokenCount", 0),
            "total_tokens": response.get("usageMetadata", {}).get("totalTokenCount", 0),
            "cached_tokens": response.get("usageMetadata", {}).get("cachedContentTokenCount", 0),
            "reasoning_tokens": response.get("usageMetadata", {}).get("thoughtsTokenCount", 0)
        },
    }


def _format_chat(response, tools_data, images):
    candidates = response.get('candidates', [{}])
    content = candidates[0].get('content', {}) if candidates else {}
    parts = content.get('parts', [])
    return {
        "data" : {
            "id" : response.get("response_id", None),
            "content" : next((p.get("text") for p in parts if not p.get("thought")), None),
            "reasoning": next((p.get("text") for p in parts if p.get("thought")), None),
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


def _format_image(response):
    data_items = response.get("data", [])
    image_urls = []
    for item in data_items:
        image_urls.append({
            "image_url": item.get("url"),
            "permanent_url": item.get("url")
        })
    return {
        "data": {
            "revised_prompt": response.get("text_content", []),
            "image_urls": image_urls,
        },
        "usage": response.get("usage_metadata", {})
    }


def _format_video(response):
    return {
        "data": {
            "content": response.get("data")[0].get("text_content"),
            "file_data": response.get("data")[0].get("file_reference"),
        }
    }
