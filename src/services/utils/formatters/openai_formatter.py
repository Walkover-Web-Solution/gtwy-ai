"""Response formatter for the OpenAI service (Responses API).

Handles all OpenAI response variants: chat (incl. inline image-generation calls
and reasoning), embeddings, and image generation.
"""

from src.services.utils.formatters.finish_reason import finish_reason_mapping
from src.services.utils.formatters.web_search_extractor import extract_web_search_annotations


def _last_message_item(output):
    result = None
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            result = item
    return result


def _last_message_item(output):
    result = None
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            result = item
    return result


def format_openai(response, tools_data, images, type="chat"):
    if type == "embedding":
        return _format_embedding(response)
    if type == "image":
        return _format_image(response)
    return _format_chat(response, tools_data, images)


def _format_chat(response, tools_data, images):
    generated_image_urls = [
        {
            "revised_prompt": item.get("revised_prompt"),
            "image_url": item.get("image_url") or item.get("permanent_url") or item.get("url"),
            "permanent_url": item.get("permanent_url") or item.get("image_url") or item.get("url"),
        }
        for item in response.get("output", [])
        if isinstance(item, dict)
        and item.get("type") == "image_generation_call"
        and (item.get("image_url") or item.get("permanent_url") or item.get("url"))
    ]
    last_message = _last_message_item(response.get("output") or [])
    llm_urls = [
        {
            "revised_prompt": item.get("revised_prompt"),
            "permanent_url": item.get("image_url") or item.get("permanent_url") or item.get("url"),
            "type": "image",
        }
        for item in response.get("output", [])
        if isinstance(item, dict)
        and item.get("type") == "image_generation_call"
        and (item.get("image_url") or item.get("permanent_url") or item.get("url"))
    ] + [
        {"revised_prompt": None, "permanent_url": f.get("gcp_url"), "filename": f.get("filename"), "type": "file"}
        for f in response.get("generated_files", []) or []
        if f.get("gcp_url")
    ]
    return {
        "data": {
            "id": response.get("id", None),
            "image_urls": generated_image_urls,
            "llm_urls": llm_urls,
            "content": (
                # Check if any item in output is a function call
                next(
                    (
                        f"Function call: {item.get('name', 'unknown')} with arguments: {item.get('arguments', '')}"
                        for item in response.get("output", [])
                        if item.get("type") == "function_call"
                    ),
                    None,
                )
                if any(item.get("type") == "function_call" for item in response.get("output", []))
                # Try to get content from multiple types with fallback
                else (
                    (last_message.get("content") or [{}])[0].get("text") if last_message else None
                )
                or (
                    next(
                        (
                            (item.get("content") or [{}])[0].get("text", None)
                            for item in response.get("output", [])
                            if item.get("type") == "output_text"
                            and (item.get("content") or [{}])[0].get("text", None) is not None
                        ),
                        None,
                    )
                )
            ),
            "reasoning": next(
                (
                    " ".join(s.get("text", "") for s in (item.get("summary") or []) if s.get("type") == "summary_text").strip() or None
                    for item in response.get("output", [])
                    if item.get("type") == "reasoning"
                ),
                None,
            ),
            "model": response.get("model", None),
            "role": "assistant",
            "finish_reason": finish_reason_mapping(response.get("status", ""))
            if response.get("status", None) == "in_progress" or response.get("status", None) == "completed"
            else finish_reason_mapping(response.get("incomplete_details", {}).get("reason", None)),
            "tools_data": tools_data or {},
            "images": images,
            "annotations": extract_web_search_annotations(response, "openai")
            or (last_message.get("content") or [{}])[0].get("annotations") if last_message else None,
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
        },
        "usage": {
            "input_tokens": response.get("usage", {}).get("input_tokens", None),
            "output_tokens": response.get("usage", {}).get("output_tokens", None),
            "total_tokens": response.get("usage", {}).get("total_tokens", None),
            "cached_tokens": response.get("usage", {}).get("input_tokens_details", {}).get('cached_tokens', None)
        }
    }


def _format_embedding(response):
    return {"data": {"embedding": response.get("data")[0].get("embedding")}}


def _format_image(response):
    image_urls = []
    for image_data in response.get("data", []):
        # image_url and permanent_url are set by image_model (GCP URL); fallback to url for both
        gcp_url = image_data.get("image_url") or image_data.get("permanent_url") or image_data.get("url")
        image_urls.append(
            {
                "revised_prompt": image_data.get("revised_prompt"),
                "image_url": gcp_url,
                "permanent_url": gcp_url,
            }
        )
    return {
        "data": {"image_urls": image_urls},
        "usage": response.get("usage", {})
    }
