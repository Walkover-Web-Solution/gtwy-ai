# from src.services.utils.unified_token_validator import validate_gemini_token_limit
from globals import logger
from src.configs.constant import GEMINI_RETRY
from src.exceptions import ApiCallError

from ..api_executor import RATE_LIMIT_STATUS_CODE, execute_api_call, retry_on_rate_limit
from google import genai
import traceback
import uuid


def is_gemini_rate_limit_error(error):
    """True for a Gemini 429 / RESOURCE_EXHAUSTED, read off the SDK error fields rather than str(error)."""
    if getattr(error, "code", None) == RATE_LIMIT_STATUS_CODE:
        return True
    if getattr(error, "status", None) == "RESOURCE_EXHAUSTED":
        return True
    payload = _error_payload(error)
    if payload.get("code") == RATE_LIMIT_STATUS_CODE or payload.get("status") == "RESOURCE_EXHAUSTED":
        return True
    return "RESOURCE_EXHAUSTED" in (getattr(error, "message", None) or "")


def _error_payload(error):
    """The parsed error body the genai SDK keeps on the exception, if it has one."""
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        inner = details.get("error")
        return inner if isinstance(inner, dict) else details
    return {}


def _gemini_retry_after(error):
    """Seconds Google asked us to wait — Retry-After header or the RetryInfo detail — else None."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is not None:
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            return _parse_seconds(retry_after)

    for detail in _error_payload(error).get("details", []) or []:
        if isinstance(detail, dict) and "RetryInfo" in str(detail.get("@type", "")):
            return _parse_seconds(detail.get("retryDelay"))
    return None


def _parse_seconds(value):
    """Parse a protobuf duration ("41s") or a plain seconds value; None when unparseable."""
    try:
        return float(str(value).rstrip("s"))
    except (TypeError, ValueError):
        return None


def gemini_error_result(error):
    """Normalise a genai exception into the failure dict, tagging 429s so they can be retried."""
    result = {"success": False, "error": str(error), "status_code": getattr(error, "code", None)}
    if is_gemini_rate_limit_error(error):
        result["status_code"] = RATE_LIMIT_STATUS_CODE
        retry_after = _gemini_retry_after(error)
        if retry_after is not None:
            result["retry_after"] = retry_after
    return result


async def call_with_rate_limit_retry(sdk_call):
    """Await `sdk_call()`, retrying Gemini 429s with backoff; any other error propagates untouched
    and the final 429 is re-raised so the caller's existing error handling stays in charge."""
    last_error = None

    async def attempt():
        nonlocal last_error
        try:
            return {"success": True, "response": await sdk_call()}
        except Exception as error:
            if not is_gemini_rate_limit_error(error):
                raise
            last_error = error
            return gemini_error_result(error)

    result = await retry_on_rate_limit(attempt, **GEMINI_RETRY)
    if not result["success"]:
        raise last_error
    return result["response"]


async def _open_gemini_stream(client, configuration):
    """Open the stream and pull its first chunk. A 429 surfaces here, before a single token has
    been yielded downstream, which is what makes the whole stream safe to retry."""
    try:
        iterator = aiter(await client.aio.models.generate_content_stream(**configuration))
        try:
            first_chunk = await anext(iterator)
        except StopAsyncIteration:
            first_chunk = None
        return {"success": True, "iterator": iterator, "first_chunk": first_chunk}
    except Exception as error:
        return gemini_error_result(error)


async def _chunks(first_chunk, iterator):
    if first_chunk is not None:
        yield first_chunk
    async for chunk in iterator:
        yield chunk


async def gemini_modelrun_stream(configuration, apiKey):
    """Async generator yielding normalised delta dicts for Gemini generate_content_stream."""
    client = genai.Client(api_key=apiKey)
    accumulated_text = ""
    accumulated_tool_calls = []
    usage = {}
    finish_reason = None
    try:
        opened = await retry_on_rate_limit(lambda: _open_gemini_stream(client, configuration), **GEMINI_RETRY)
        if not opened["success"]:
            yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": opened["error"]}
            return
        async for chunk in _chunks(opened["first_chunk"], opened["iterator"]):
            chunk_dict = chunk.model_dump()
            candidates = chunk_dict.get("candidates", [])
            usage_meta = chunk_dict.get("usage_metadata")
            if usage_meta:
                usage = {
                    "prompt_token_count": usage_meta.get("prompt_token_count", 0),
                    "candidates_token_count": usage_meta.get("candidates_token_count", 0),
                    "total_token_count": usage_meta.get("total_token_count", 0),
                    "cached_content_token_count": usage_meta.get("cached_content_token_count", 0),
                    "thoughts_token_count": usage_meta.get("thoughts_token_count", 0),
                }
            if not candidates:
                continue
            candidate = candidates[0]
            finish_reason = candidate.get("finish_reason") or finish_reason
            parts = candidate.get("content", {}).get("parts", []) or []
            for part in parts:
                if part.get("text"):
                    if part.get("thought") is True:
                        yield {
                            "content": None,
                            "tool_calls": None,
                            "usage": None,
                            "finish_reason": None,
                            "reasoning": part["text"],
                        }
                    else:
                        accumulated_text += part["text"]
                        yield {
                            "content": part["text"],
                            "tool_calls": None,
                            "usage": None,
                            "finish_reason": None,
                            "reasoning": None,
                        }
                if part.get("function_call"):
                    fc = part["function_call"]
                    fc["id"] = f"gemini_fc_{str(uuid.uuid4())}"
                    accumulated_tool_calls.append(fc)
        yield {"content": None, "tool_calls": accumulated_tool_calls or None, "usage": usage, "finish_reason": finish_reason, "reasoning": None}
    except Exception as error:
        yield {"content": None, "tool_calls": None, "usage": {}, "finish_reason": "error", "reasoning": None, "error": str(error)}


async def gemini_modelrun(
    configuration,
    apiKey,
    execution_time_logs,
    bridge_id,
    timer,
    message_id=None,
    org_id=None,
    name="",
    org_name="",
    service="",
    count=0,
    token_calculator=None,
):
    try:
        # Validate token count before making API call
        # model_name = configuration.get('model')
        # validate_gemini_token_limit(configuration, model_name, service, apiKey)
        
        client = genai.Client(api_key=apiKey) 

        # Define the API call function
        async def api_call(config):
            try:
                chat_completion = await client.aio.models.generate_content(**config)
                return {'success': True, 'response': chat_completion.model_dump()}
            except Exception as error:
                return gemini_error_result(error)

        # Execute API call with monitoring
        return await execute_api_call(
            configuration=configuration,
            api_call=api_call,
            execution_time_logs=execution_time_logs,
            timer=timer,
            bridge_id=bridge_id,
            message_id=message_id,
            org_id=org_id,
            alert_on_retry=True,
            name=name,
            org_name=org_name,
            service=service,
            count=count,
            token_calculator=token_calculator,
            # 429s are retried on this model first; the model fallback only kicks in once they run out.
            retry_policy=GEMINI_RETRY,
        )

    except Exception as error:
        execution_time_logs.append(
            {
                "step": f"{service} Processing time for call :- {count + 1}",
                "time_taken": timer.stop("API chat completion"),
            }
        )
        raise ApiCallError(str(error), status_code=getattr(error, "status_code", None), service=service) from error
