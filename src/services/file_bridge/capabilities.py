"""Which file kinds a service/model pair can take as-is.

Two sources, both must agree:
  * what the provider handler actually sends today. The DB flags say nothing
    about handler code, and several handlers drop attachments the model could
    read (Groq drops all, Gemini ignores current-turn documents, ...).
  * the model's validationConfig flags: ``vision`` gates images, ``files``
    gates PDFs. A missing flag trusts the handler; an explicit False wins.

Current turn and history are separate because the history builders differ from
the current-turn code (the OpenAI-compatible builders read ``urls``, which
history rows never have, so they send no history attachments at all).
"""

from src.configs.service_registry import uses_openai_sdk
from src.services.file_bridge.detect import AUDIO, IMAGE, PDF, VIDEO

_CURRENT_TURN = {
    "openai": {IMAGE, PDF},
    "anthropic": {IMAGE, PDF},
    "gemini": {IMAGE, AUDIO},
    "mistral": {IMAGE},
    "grok": {IMAGE},
    "deepseek": {IMAGE},
    "groq": set(),
    "deepgram": {AUDIO},
}

_HISTORY = {
    "openai": {IMAGE, PDF},
    "anthropic": {IMAGE, PDF},
    "gemini": {IMAGE, PDF, AUDIO, VIDEO},
}

_FLAG_FOR_KIND = {IMAGE: "vision", PDF: "files"}


def _validation_config(service: str, model: str) -> dict:
    # Call-time import: model_configuration pulls in baseService at import time.
    from src.configs.model_configuration import model_config_document

    return ((model_config_document.get(service) or {}).get(model) or {}).get("validationConfig") or {}


def _handler_kinds(table: dict, service: str) -> set:
    if service in table:
        return set(table[service])
    if table is _CURRENT_TURN and uses_openai_sdk(service):
        # open_router / neev_cloud / moonshot / openai_completion
        return {IMAGE}
    return set()


def _apply_model_flags(kinds: set, service: str, model: str) -> set:
    flags = _validation_config(service, model)
    return {kind for kind in kinds if flags.get(_FLAG_FOR_KIND.get(kind, ""), True) is not False}


def native_kinds(service: str, model: str) -> set:
    """Kinds the current turn can send to this model without conversion."""
    return _apply_model_flags(_handler_kinds(_CURRENT_TURN, service), service, model)


def native_history_kinds(service: str, model: str) -> set:
    """Kinds the history builder for this service sends to this model."""
    return _apply_model_flags(_handler_kinds(_HISTORY, service), service, model)


def supports_tools(service: str, model: str) -> bool:
    return _validation_config(service, model).get("tools", True) is not False


def context_window(service: str, model: str) -> int | None:
    value = _validation_config(service, model).get("context_window")
    return value if isinstance(value, int) and value > 0 else None
