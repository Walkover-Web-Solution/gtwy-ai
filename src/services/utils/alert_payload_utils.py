"""Make an LLM request payload safe and small enough to attach to an alert."""

import json
from typing import Any

from globals import logger

# Slack caps a message at ~40k chars; leave room for the rest of the alert.
MAX_PAYLOAD_CHARS = 15000
MAX_STRING_CHARS = 2000

SECRET_KEY_PARTS = ("apikey", "api_key", "authorization", "token", "secret", "password")


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower().replace("-", "_")
    # "max_tokens" / "max_output_tokens" are config, not credentials.
    if lowered.endswith("tokens"):
        return False
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if _is_secret_key(k) else _clean_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    if isinstance(value, str):
        # Inline files (image_url / input_file data URLs) are base64 blobs.
        if value.startswith("data:") and ";base64," in value:
            return f"{value.split(';base64,', 1)[0]};base64,<{len(value)} chars omitted>"
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}…<{len(value) - MAX_STRING_CHARS} chars truncated>"
    return value


def sanitize_payload_for_alert(payload: Any) -> str | None:
    """Return the payload as a pretty JSON string (redacted, base64-stripped, size-capped), or None."""
    if not payload:
        return None
    try:
        payload_text = json.dumps(_clean_value(payload), indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"sanitize_payload_for_alert: could not serialize request payload: {e}")
        return f"<could not serialize request payload: {e}>"
    if len(payload_text) > MAX_PAYLOAD_CHARS:
        payload_text = f"{payload_text[:MAX_PAYLOAD_CHARS]}\n…<{len(payload_text) - MAX_PAYLOAD_CHARS} chars truncated>"
    return payload_text
