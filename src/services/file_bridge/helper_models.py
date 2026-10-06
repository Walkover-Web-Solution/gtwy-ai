"""Helper model calls for files that need a model to read them.

Gemini reads images, video and scanned PDFs; Deepgram transcribes audio. The
org's own key is used when it saved one, otherwise the platform key, and each
call's cost is logged to the request's usage log for billing.
"""

import asyncio
import io

from config import Config
from src.services.file_bridge.context import FileBridgeContext
from src.services.utils.logger import logger

# Gemini takes inline parts up to ~20 MB per request; larger files go through the Files API.
GEMINI_INLINE_LIMIT_BYTES = 18 * 1024 * 1024
GEMINI_FILE_POLL_SECONDS = 2
GEMINI_FILE_POLL_ATTEMPTS = 60

IMAGE_PROMPT = (
    "Describe this image in full detail for someone who cannot see it. "
    "Copy any text in it exactly. Describe charts, tables and diagrams with their numbers and labels."
)
VIDEO_PROMPT = (
    "Give a detailed summary of this video, then a transcript of everything said in it. Note any on-screen text."
)
SCANNED_PDF_PROMPT = "Extract all text from this document exactly, page by page. Describe any images or charts."


class HelperUnavailableError(Exception):
    pass


def resolve_key(service: str, ctx: FileBridgeContext) -> tuple[str, str]:
    """Return (api_key, source) with source "org" or "platform"."""
    from src.services.billing.billing_utils import get_platform_apikey

    platform_key = get_platform_apikey(service)
    own_key = (ctx.service_apikeys or {}).get(service)
    # service_apikeys also holds the request's main key, which is the platform key
    # on wallet traffic, so compare before calling it the org's own.
    if own_key and own_key != platform_key:
        return own_key, "org"
    if platform_key:
        return platform_key, "platform"
    raise HelperUnavailableError(f"no {service} key available to read this file")


def _cost(service: str, model: str, model_response: dict) -> dict:
    from src.configs.model_configuration import model_config_document
    from src.services.utils.token_calculation import TokenCalculator

    calculator = TokenCalculator(service, None)
    usage = calculator.calculate_usage(model_response)
    try:
        total_cost = calculator.calculate_total_cost(model, service).get("total_cost") or 0
    except Exception:
        if model not in (model_config_document.get(service) or {}):
            logger.error(f"[file_bridge] no pricing for {service}/{model}; helper call logged at cost 0")
        total_cost = 0
    return {"usage": usage, "cost": total_cost}


def _log(ctx: FileBridgeContext, service: str, model: str, key_source: str, url: str, priced: dict) -> None:
    ctx.usage_log.append({"service": service, "model": model, "key_source": key_source, "url": url, **priced})


async def _gemini_part(client, data: bytes, mime_type: str):
    from google.genai import types

    if len(data) <= GEMINI_INLINE_LIMIT_BYTES:
        return types.Part.from_bytes(data=data, mime_type=mime_type)
    uploaded = await client.aio.files.upload(file=io.BytesIO(data), config={"mime_type": mime_type})
    for _ in range(GEMINI_FILE_POLL_ATTEMPTS):
        state = getattr(getattr(uploaded, "state", None), "name", None) or str(getattr(uploaded, "state", ""))
        if "ACTIVE" in state:
            break
        if "FAILED" in state:
            raise RuntimeError("Gemini could not process the uploaded file")
        await asyncio.sleep(GEMINI_FILE_POLL_SECONDS)
        uploaded = await client.aio.files.get(name=uploaded.name)
    return types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type)


async def gemini_read(data: bytes, mime_type: str, prompt: str, url: str, ctx: FileBridgeContext) -> str:
    from google import genai
    from google.genai import types

    api_key, key_source = resolve_key("gemini", ctx)
    model = Config.FILE_BRIDGE_GEMINI_MODEL
    client = genai.Client(api_key=api_key)
    part = await _gemini_part(client, data, mime_type)
    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[part, types.Part(text=prompt)])],
    )
    usage_metadata = response.usage_metadata.model_dump() if response.usage_metadata else {}
    _log(ctx, "gemini", model, key_source, url, _cost("gemini", model, {"usage_metadata": usage_metadata}))
    return (response.text or "").strip()


async def deepgram_transcribe(url: str, ctx: FileBridgeContext) -> str:
    from src.services.commonServices.deepgram.deepgramModelRun import _get_deepgram_client

    api_key, key_source = resolve_key("deepgram", ctx)
    model = Config.FILE_BRIDGE_DEEPGRAM_MODEL
    response = await _get_deepgram_client(api_key).listen.v1.media.transcribe_url(
        url=url, model=model, smart_format=True
    )
    response_dict = response.model_dump()
    channels = (response_dict.get("results") or {}).get("channels") or []
    alternatives = channels[0].get("alternatives", []) if channels else []
    transcript = alternatives[0].get("transcript", "") if alternatives else ""
    _log(ctx, "deepgram", model, key_source, url, _cost("deepgram", model, response_dict))
    return (transcript or "").strip()
