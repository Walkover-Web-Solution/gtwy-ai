"""Response formatter for the Deepgram service (audio transcription)."""

from src.services.utils.formatters.finish_reason import finish_reason_mapping


def format_deepgram(response, tools_data, images=None):
    metadata = response.get("metadata", {}) or {}
    results = response.get("results", {}) or {}
    channels = results.get("channels", [])
    alternatives = channels[0].get("alternatives", []) if channels else []
    transcript = (alternatives[0].get("transcript") if alternatives else None) or ""

    model_id = (metadata.get("models") or [None])[0]
    model_info = (metadata.get("model_info") or {}).get(model_id, {}) if model_id else {}
    model_label = model_info.get("name") or model_id

    return {
        "data": {
            "id": metadata.get("request_id", None),
            "content": transcript,
            "model": model_label,
            "role": "assistant",
            "tools_data": tools_data or {},
            "images": images,
            "annotations": None,
            "fallback": response.get("fallback") or False,
            "firstAttemptError": response.get("firstAttemptError") or "",
            "finish_reason": finish_reason_mapping("stop"),
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "audio_duration": metadata.get("duration", 0),
        },
    }
