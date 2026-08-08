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

The DB is the single source of truth for all service configurations.
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

# Runtime registry, refreshed from Mongo. The DB is the source of truth.
service_registry_document = {}


# ---------------------------------------------------------------------------
# Lookup helpers (registry only)
# ---------------------------------------------------------------------------
def get_service(name):
    """Return the registry doc for ``name``, or None if unknown.

    The DB is the source of truth; no fallback to hardcoded values.
    """
    return service_registry_document.get(name)


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


def web_search_tool_config(name):
    if wire_format(name) in ("openai_chat", "openai_responses"):
        return {
            "unfiltered": {"type": "web_search_preview"},
            "filtered": {"type": "web_search"},
        }
    return None


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
