import json
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

# Utility and helper functions for remapping/refactoring the incoming request

def convert_bearer_to_local_auth(request: Request) -> None:
    """
    Open AI sends pauth-key as Bearer token in the header.
    This function extracts and refactors the header.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header with Bearer <pauthkey> is required.",
        )

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token cannot be empty.")
    
    raw_headers = list(request.scope.get("headers", []))
    filtered_headers = [
        (name, value)
        for name, value in raw_headers
        if name.lower() != b"authorization"
    ]
    filtered_headers.append((b"pauthkey", token.encode("utf-8")))
    request.scope["headers"] = filtered_headers
    if "_headers" in request.__dict__:
        del request.__dict__["_headers"]


def _normalize_message_content(content: Any) -> Optional[str]:
    if isinstance(content, str):
        content = content.strip()
        return content or None

    if isinstance(content, list):
        text_parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = (item.get("type") or "").lower()
                if item_type in {"text", "input_text"}:
                    text_value = (item.get("text") or "").strip()
                    if text_value:
                        text_parts.append(text_value)
        merged = "\n".join(text_parts).strip()
        return merged or None

    return None


def _extract_text_from_input(input_value: Any) -> Optional[str]:
    if isinstance(input_value, str):
        text = input_value.strip()
        return text or None

    if isinstance(input_value, dict):
        return _normalize_message_content(input_value.get("content"))

    if isinstance(input_value, list):
        segments: List[str] = []
        for chunk in input_value:
            if isinstance(chunk, dict):
                content = chunk.get("content")
                extracted = _normalize_message_content(content)
                if extracted:
                    segments.append(extracted)
                elif isinstance(chunk.get("text"), str):
                    text_value = chunk["text"].strip()
                    if text_value:
                        segments.append(text_value)
        merged = "\n".join(segments).strip()
        return merged or None

    return None


def _extract_agent_identifier(payload: Dict[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    agent_id = agent_id.strip()
    if not agent_id:
        raise HTTPException(
            status_code=400,
            detail="`agent_id` must be included in the request body.",
        )
    return agent_id


async def build_and_override_request_body(request: Request) -> None:
    payload = await request.json()
    agent_id = _extract_agent_identifier(payload)
    llm_model = payload.get("model")

    user_message = _extract_text_from_input(payload.get("input"))

    if not user_message:
        raise HTTPException(status_code=400, detail="No user message found in payload.")

    configuration = payload.get("configuration") or {}

    if isinstance(llm_model, str) and llm_model.strip():
        configuration.setdefault("model", llm_model.strip())

    internal_body: Dict[str, Any] = {
        "agent_id": agent_id,
        "bridge_id": agent_id,

        "user": user_message,
        "messages": payload.get("messages", []),
        "thread_id": payload.get("conversation_id")
        or payload.get("thread_id") or None,
        "sub_thread_id": payload.get("sub_thread_id") or None,
        "variables": payload.get("variables") or {},
        "configuration": configuration,
        "attachments": payload.get("attachments", []),
    }

    body_bytes = json.dumps(internal_body).encode("utf-8")
    request._body = body_bytes  # type: ignore[attr-defined]
    request._json = internal_body  # type: ignore[attr-defined]
    request._stream_consumed = True  # type: ignore[attr-defined]
    if "_form" in request.__dict__:
        request.__dict__.pop("_form")


# Response building Utils:

def _build_output_blocks(message_content: str) -> List[Dict[str, Any]]:
    reasoning_block = {
        "id": f"rs_{uuid.uuid4().hex}",
        "type": "reasoning",
        "summary": [],
    }

    message_block = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": message_content,
            }
        ],
    }

    return [reasoning_block, message_block]


def format_openai_response(chat_response: Dict[str, Any], original_payload: Dict[str, Any] | None) -> Dict[str, Any]:
    response_data = chat_response.get("response", {}).get("data", {})
    usage_data = chat_response.get("response", {}).get("usage", {}) or {}

    message_content = response_data.get("content")
    if isinstance(message_content, list):
        message_content = "\n".join(
            chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
            for chunk in message_content
        ).strip()
    elif not isinstance(message_content, str):
        message_content = str(message_content or "")

    message_content = message_content.strip()
    finish_reason = response_data.get("finish_reason") or usage_data.get("finish_reason")
    model = response_data.get("model") if isinstance(response_data, dict) else None

    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())

    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": _build_output_blocks(message_content),
        "usage": {
            "input_tokens": usage_data.get("input_tokens"),
            "input_tokens_details": {
                "cached_tokens": usage_data.get("cached_input_tokens", 0),
            },
            "output_tokens": usage_data.get("output_tokens"),
            "output_tokens_details": {
                "reasoning_tokens": usage_data.get("reasoning_tokens"),
            },
            "total_tokens": usage_data.get("total_tokens"),
        },
        "user": original_payload.get("user") if isinstance(original_payload, dict) else None,
        "output_text": message_content,
        "finish_reason": finish_reason or "stop",
    }


async def run_openai_chat_and_format(
    request: Request,
    db_config: Dict[str, Any],
    chat_handler: Callable[[Request, Dict[str, Any]], Awaitable[Any]],
) -> Dict[str, Any]:
    openai_payload = getattr(request.state, "openai_payload", {})
    try:
        internal_response = await chat_handler(request, db_config)
        if isinstance(internal_response, JSONResponse):
            content = internal_response.body
            try:
                content_dict = json.loads(content)
            except Exception:
                content_dict = {}
            chat_response = content_dict
        else:
            chat_response = internal_response

        return format_openai_response(chat_response, openai_payload)
    except HTTPException as err:
        raise HTTPException(
            status_code=400,
            detail=err
        )
