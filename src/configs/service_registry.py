"""DB-driven service registry for AI services.

Loads per-service capability metadata (base_url, wire_format, client, default
model, parameter mapping, api-key status codes) from the Mongo `services`
collection and exposes named capability predicates used across the codebase to
decide service-specific behavior.

Design notes
------------
Two orthogonal concepts are modeled explicitly so that scattered ``service ==
...`` allow-lists can be replaced by *named capabilities* rather than one
identity predicate:

- ``wire_format`` — the request/response shape (``openai_chat`` covers the
  OpenAI Chat Completions ``choices[0].message`` shape).
- ``client`` — which SDK actually makes the call (only ``openai_sdk`` services
  can share the generic ``AsyncOpenAI`` runner; groq/grok/mistral share the
  wire format but use their own clients).

Every accessor falls back to ``_FALLBACK_REGISTRY`` (the hardcoded source of
truth, mirroring today's behavior) when the DB registry is empty or a service
is missing, so a registry miss never hard-fails a request. ``_FALLBACK_REGISTRY``
is also the source the seed migration uses to populate the collection.
"""

import asyncio

from pymongo.errors import OperationFailure, PyMongoError

from config import Config
from globals import logger
from models.mongo_connection import db
from src.services.utils.load_service_configs import get_service_configs

# NOTE: send_message is imported lazily inside _async_change_listener to avoid a
# circular import — baseService.utils now imports capability predicates from this
# module, so importing it at module load time would form a cycle.

service_config_model = db["services"]

# Runtime registry, refreshed from Mongo. Falls back to _FALLBACK_REGISTRY per-field.
service_registry_document = {}


# ---------------------------------------------------------------------------
# Hardcoded source of truth (runtime fallback + seed migration source)
# ---------------------------------------------------------------------------
# wire_format: openai_chat | openai_responses | anthropic | gemini | deepgram
# client:      openai_sdk | groq_sdk | grok_http | mistral_sdk |
#              anthropic_sdk | gemini_sdk | deepgram_sdk
_FALLBACK_REGISTRY = {
    "openai": {
        "service_name": "openai",
        "base_url": None,
        "wire_format": "openai_responses",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": True,
        "default_model": "gpt-4o",
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    "openai_completion": {
        "service_name": "openai_completion",
        "base_url": None,  # AsyncOpenAI default -> https://api.openai.com/v1
        "wire_format": "openai_chat",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": False,
        "default_model": None,
        "prompt_role": "developer",  # openai_completion sends the system prompt as role "developer"
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    "gemini": {
        "service_name": "gemini",
        "base_url": None,
        "wire_format": "gemini",
        "client": "gemini_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": False,
        "supports_reasoning": True,
        "default_model": "gemini-2.5-flash",
        "apikey_status_codes": {"invalid": [400], "unauthorized": [403], "limited": [429]},
    },
    "anthropic": {
        "service_name": "anthropic",
        "base_url": None,
        "wire_format": "anthropic",
        "client": "anthropic_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": False,
        "supports_reasoning": True,
        "default_model": "claude-3-7-sonnet-latest",
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    "groq": {
        "service_name": "groq",
        "base_url": None,
        "wire_format": "openai_chat",
        "client": "groq_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": False,
        "default_model": "llama-3.3-70b-versatile",
        "apikey_status_codes": {"invalid": [400, 401], "unauthorized": [403], "limited": [422, 429, 498]},
    },
    "grok": {
        "service_name": "grok",
        "base_url": "https://api.x.ai/v1",
        "wire_format": "openai_chat",
        "client": "grok_http",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": False,
        "default_model": "grok-4-fast",
        "apikey_status_codes": {"invalid": [400, 401], "unauthorized": [403], "limited": [429]},
    },
    "open_router": {
        "service_name": "open_router",
        "base_url": "https://openrouter.ai/api/v1",
        "wire_format": "openai_chat",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": False,
        "supports_reasoning": False,
        "default_model": "deepseek/deepseek-chat-v3-0324:free",
        "prompt_role": "developer",  # open_router sends the system prompt as role "developer"
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [402, 429]},
    },
    "mistral": {
        "service_name": "mistral",
        "base_url": None,
        "wire_format": "openai_chat",
        "client": "mistral_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": False,
        "supports_reasoning": False,
        "default_model": "mistral-medium-latest",
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    "deepgram": {
        "service_name": "deepgram",
        "base_url": None,
        "wire_format": "deepgram",
        "client": "deepgram_sdk",
        "supports_streaming": False,
        "supports_tool_calls": False,
        "supports_stream_usage": False,
        "supports_reasoning": False,
        "default_model": "nova-3",
        "apikey_status_codes": {"invalid": [400, 401, 404], "unauthorized": [403], "limited": [402, 413, 422, 429]},
    },
    "neev_cloud": {
        "service_name": "neev_cloud",
        "base_url": "https://inference.ai.neevcloud.com/v1",
        "wire_format": "openai_chat",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": False,
        "supports_reasoning": False,
        "default_model": "gpt-oss-120b",
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    "moonshot": {
        "service_name": "moonshot",
        "base_url": "https://api.moonshot.ai/v1",
        "wire_format": "openai_chat",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": True,
        "default_model": "kimi-k2.6",
        "apikey_status_codes": {"invalid": [401], "unauthorized": [403], "limited": [429]},
    },
    # deepseek is openai_chat + openai_sdk -> routes through the generic runner.
    # Its old per-service stream emitted content+reasoning in a single combined
    # yield; the generic runner emits them as separate yields. The final
    # accumulated response is identical (validated under the golden harness).
    "deepseek": {
        "service_name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "wire_format": "openai_chat",
        "client": "openai_sdk",
        "supports_streaming": True,
        "supports_tool_calls": True,
        "supports_stream_usage": True,
        "supports_reasoning": True,
        "default_model": "deepseek-v4-flash",
        "apikey_status_codes": {"invalid": [400, 401], "unauthorized": [403], "limited": [429]},
    },
}


# ---------------------------------------------------------------------------
# Lookup helpers (registry first, hardcoded fallback second)
# ---------------------------------------------------------------------------
def get_service(name):
    """Return the registry doc for ``name``, or None if unknown.

    Prefers the live DB document, then merges over the hardcoded fallback so a
    partially-specified DB row still resolves every field.
    """
    fallback = _FALLBACK_REGISTRY.get(name)
    live = service_registry_document.get(name)
    if live is None and fallback is None:
        return None
    if live is None:
        return fallback
    if fallback is None:
        return live
    merged = {**fallback, **{k: v for k, v in live.items() if v is not None}}
    return merged


def _field(name, key, default=None):
    svc = get_service(name)
    if svc is None:
        return default
    value = svc.get(key)
    return default if value is None else value


def wire_format(name):
    return _field(name, "wire_format")


def client(name):
    return _field(name, "client")


def base_url(name):
    return _field(name, "base_url")


def default_model(name):
    return _field(name, "default_model")


def prompt_role(name):
    """Role used for the system prompt message. Defaults to "system";
    open_router uses "developer"."""
    return _field(name, "prompt_role", "system")


# ---------------------------------------------------------------------------
# Capability predicates — each maps to a specific allow-list set (see plan §1.1)
# ---------------------------------------------------------------------------
def uses_openai_sdk(name):
    """Set C — services callable via a generic AsyncOpenAI(base_url=...) runner."""
    return client(name) == "openai_sdk" and wire_format(name) == "openai_chat"


def has_openai_choices_shape(name):
    """Set A — response uses the OpenAI ``choices[0].message`` shape."""
    return wire_format(name) == "openai_chat"


def uses_string_tool_choice(name):
    """Set G — services that accept a string/function-style ``tool_choice``.

    The openai_chat services plus openai (openai_responses): both families take
    a string tool_choice ("none"/"auto"/function name), unlike anthropic/gemini
    which use structured objects.
    """
    return wire_format(name) in ("openai_chat", "openai_responses")


def supports_streaming(name):
    return bool(_field(name, "supports_streaming", False))


def supports_tool_calls(name):
    """Service can return tool/function calls (everything except audio-only deepgram)."""
    return bool(_field(name, "supports_tool_calls", False))


def supports_stream_usage(name):
    return bool(_field(name, "supports_stream_usage", False))


def supports_reasoning(name):
    return bool(_field(name, "supports_reasoning", False))


def apikey_status_codes(name):
    """Return the per-status HTTP code map for ``name`` (with safe default)."""
    return _field(name, "apikey_status_codes", {})


# ---------------------------------------------------------------------------
# Lifecycle: init + change-stream listener (mirrors model_configuration.py)
# ---------------------------------------------------------------------------
async def init_service_registry():
    """Initialize or refresh the in-memory service registry from Mongo."""
    global service_registry_document
    try:
        new_document = await get_service_configs()
        service_registry_document.clear()
        service_registry_document.update(new_document)
        logger.info(f"Service registry refreshed successfully ({len(service_registry_document)} services).")
    except Exception as e:
        logger.error(f"Error refreshing service registry: {e}")


async def _async_change_listener():
    from src.services.commonServices.baseService.utils import send_message  # lazy: avoids import cycle

    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
    async with service_config_model.watch(pipeline, full_document="updateLookup") as stream:
        logger.info("MongoDB change stream is now listening for service registry changes.")
        async for change in stream:
            logger.info(f"Change detected in service registry: {change['operationType']}")
            await init_service_registry()
            await send_message(
                cred={"apikey": Config.RTLAYER_AUTH, "ttl": 1, "channel": "global_model_updates"},
                data={
                    "event": "service_registry_updated",
                    "operation": change["operationType"],
                    "service": change.get("fullDocument", {}).get("service_name"),
                    "timestamp": str(change.get("clusterTime", "")),
                },
            )
            logger.info("Service registry change detected and sent to RTLayer successfully.")


async def background_listen_for_service_changes():
    """Background task: change-stream listener with a retry/reconnect loop."""
    while True:
        try:
            await _async_change_listener()
        except (OperationFailure, PyMongoError) as e:
            logger.error(f"Service registry change stream error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error in service registry listener: {e}. Restarting in 10 seconds...")
            await asyncio.sleep(10)
