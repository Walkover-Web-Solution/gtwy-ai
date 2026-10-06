"""File bridge: give a model the files it cannot read itself, as text.

``prepare`` runs in Helper.create_service_handler, right before the provider
handler is built, so the primary call, both fallback paths and the reviewer all
go through it. For every attachment the chosen model cannot take natively it
converts the file to text (cached by URL) and puts that text in the message.
"""

import asyncio
import io
import math
from html import escape

from config import Config
from src.configs.constant import inbuild_tools
from src.services.file_bridge import capabilities, converters, helper_models
from src.services.file_bridge.cache import get_cached, get_or_convert
from src.services.file_bridge.context import FileBridgeContext
from src.services.file_bridge.detect import (
    AUDIO,
    CSV,
    DOCX,
    IMAGE,
    LEGACY_OFFICE,
    PDF,
    PPTX,
    TEXT,
    UNKNOWN,
    VIDEO,
    XLSX,
    detect_kind,
    file_name,
    url_extension,
)
from src.services.file_bridge.download import download_file
from src.services.utils.logger import logger

MAX_REQUEST_CHARS = 100_000
# Share of the model's context window that attachment text may take.
CONTEXT_SHARE = 0.25
CHARS_PER_TOKEN = 4
CONVERT_CONCURRENCY = 4

GEMINI_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}

READER_TOOL = inbuild_tools["Gtwy_File_Reader"]


def part_size() -> int:
    return Config.FILE_BRIDGE_MAX_CHARS_PER_FILE


# ---------------------------------------------------------------- conversion


def image_for_gemini(data: bytes, content_type: str | None) -> tuple[bytes, str]:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in GEMINI_IMAGE_MIME_TYPES:
        return data, mime
    # gif / bmp / tiff and unlabelled images: re-encode as PNG.
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        out = io.BytesIO()
        image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(out, format="PNG")
        return out.getvalue(), "image/png"


async def _convert_uncached(url: str, kind: str, ctx: FileBridgeContext) -> tuple[str, str]:
    """Return (text, kind). Raises on failure."""
    if kind == AUDIO:
        # Deepgram fetches the URL itself; no need to download it here.
        return await helper_models.deepgram_transcribe(url, ctx), kind
    if kind == LEGACY_OFFICE:
        raise ValueError("old Office formats (.doc, .xls, .ppt) are not supported; use .docx, .xlsx, .pptx or PDF")

    data, content_type = await download_file(url)
    if kind == UNKNOWN:
        kind = detect_kind(url, None, content_type)

    if kind == TEXT:
        return converters.convert_text(data), kind
    if kind == CSV:
        return converters.convert_csv(data, "\t" if url_extension(url) == ".tsv" else None), kind
    if kind == XLSX:
        return converters.convert_xlsx(data), kind
    if kind == DOCX:
        return converters.convert_docx(data), kind
    if kind == PPTX:
        return converters.convert_pptx(data), kind
    if kind == PDF:
        text, looks_scanned = converters.convert_pdf(data)
        if looks_scanned:
            try:
                text = await helper_models.gemini_read(
                    data, "application/pdf", helper_models.SCANNED_PDF_PROMPT, url, ctx
                )
            except helper_models.HelperUnavailableError as exc:
                logger.warning(f"[file_bridge] scanned PDF kept as plain text: {exc}")
        return text, kind
    if kind == IMAGE:
        image_bytes, mime = image_for_gemini(data, content_type)
        return await helper_models.gemini_read(image_bytes, mime, helper_models.IMAGE_PROMPT, url, ctx), kind
    if kind == VIDEO:
        mime = (content_type or "").split(";")[0].strip() or "video/mp4"
        return await helper_models.gemini_read(data, mime, helper_models.VIDEO_PROMPT, url, ctx), kind
    raise ValueError("this file type cannot be read")


async def convert_url(url: str, kind: str, ctx: FileBridgeContext) -> dict:
    """Text of ``url`` as {"text", "kind"} or {"error", "kind"}; cached by URL and never raises."""

    async def _convert():
        try:
            text, final_kind = await _convert_uncached(url, kind, ctx)
            return {"text": text, "kind": final_kind}
        except Exception as exc:
            logger.error(f"[file_bridge] could not convert {file_name(url)} ({kind}): {exc}")
            return {"text": "", "kind": kind, "error": str(exc) or exc.__class__.__name__}

    try:
        return await get_or_convert(url, _convert)
    except Exception as exc:
        logger.error(f"[file_bridge] conversion cache failed for {file_name(url)}: {exc}")
        return {"text": "", "kind": kind, "error": "could not read file"}


# ---------------------------------------------------------------- prompt text


def _block(url: str, kind: str, text: str, limit: int, error: str | None = None) -> tuple[str, bool]:
    """Wrap a file's text for the prompt. Returns (block, truncated)."""
    attrs = f'name="{escape(file_name(url))}" type="{kind}" url="{escape(url)}"'
    if error:
        return f'<attached_file {attrs} error="{escape(error)}"/>', False
    truncated = len(text) > limit
    if not truncated:
        return f"<attached_file {attrs}>\n{text}\n</attached_file>", False
    parts = math.ceil(len(text) / part_size())
    note = (
        f"[Only the first {limit} of {len(text)} characters are shown. "
        f'Call {READER_TOOL} with url="{url}" and part=2 (of {parts}) to read more.]'
    )
    return f'<attached_file {attrs} truncated="true">\n{text[:limit]}\n{note}\n</attached_file>', True


def _request_budget(service: str, model: str) -> int:
    window = capabilities.context_window(service, model)
    if not window:
        return MAX_REQUEST_CHARS
    return min(MAX_REQUEST_CHARS, int(window * CHARS_PER_TOKEN * CONTEXT_SHARE))


# ---------------------------------------------------------------- attachments


def _current_attachments(params: dict) -> list[dict]:
    declared = {
        item.get("url"): item.get("type")
        for item in params.get("user_urls") or []
        if isinstance(item, dict) and item.get("url")
    }
    seen = set()
    attachments = []
    for bucket, default_type in (("images", "image"), ("audios", "audio"), ("files", None)):
        for url in params.get(bucket) or []:
            if not isinstance(url, str) or not url or url in seen:
                continue
            seen.add(url)
            declared_type = declared.get(url) or default_type
            attachments.append(
                {"url": url, "bucket": bucket, "declared": declared_type, "kind": detect_kind(url, declared_type)}
            )
    return attachments


def _saved_type(attachment: dict) -> str:
    """The user_urls type to store in history: what the user sent, else the detected kind."""
    if attachment["declared"] in ("image", "audio", "video", "pdf", "file"):
        return attachment["declared"]
    return attachment["kind"] if attachment["kind"] in (IMAGE, AUDIO, VIDEO, PDF) else "file"


async def _bridge_history(history: list, service: str, model: str, ctx: FileBridgeContext, budget: int):
    """Return (new_history, budget_left, needs_tool). Never changes ``history`` itself."""
    native = capabilities.native_history_kinds(service, model)
    new_history = []
    needs_tool = False
    for message in history:
        urls = message.get("user_urls") if isinstance(message, dict) else None
        if not urls or message.get("role") != "user":
            new_history.append(message)
            continue

        keep, extra, seen = [], [], set()
        for item in urls:
            url = item.get("url") if isinstance(item, dict) else None
            if not url or url in seen:
                continue
            seen.add(url)
            kind = detect_kind(url, item.get("type"))
            ctx.allowed_urls.add(url)
            ctx.url_kinds[url] = kind
            if kind in native:
                keep.append(item)
                continue
            cached = await get_cached(url)
            if cached and cached.get("text") and budget > 0:
                block, truncated = _block(url, cached.get("kind") or kind, cached["text"], min(part_size(), budget))
                budget -= len(block)
                needs_tool = needs_tool or truncated
                extra.append(block)
            else:
                needs_tool = True
                extra.append(
                    f'[Earlier attachment {file_name(url)} ({kind}); call {READER_TOOL} with url="{url}" to read it]'
                )

        if not extra:
            new_history.append(message)
            continue
        content = message.get("content") or ""
        new_history.append({**message, "user_urls": keep, "content": "\n\n".join([content, *extra]).strip()})
    return new_history, budget, needs_tool


def _add_reader_tool(params: dict, service: str, model: str) -> None:
    if not capabilities.supports_tools(service, model):
        return
    from src.services.utils.built_in_tools.file_reader import build_file_reader_tool_schema

    custom_config = dict(params.get("customConfig") or {})
    tools = list(custom_config.get("tools") or [])
    if any(isinstance(t, dict) and t.get("name") == READER_TOOL for t in tools):
        return
    tools.append(build_file_reader_tool_schema())
    custom_config["tools"] = tools
    params["customConfig"] = custom_config
    mapping = dict(params.get("tool_id_and_name_mapping") or {})
    mapping[READER_TOOL] = {"type": READER_TOOL, "name": READER_TOOL}
    params["tool_id_and_name_mapping"] = mapping


async def prepare(params: dict, service: str) -> dict:
    """Return params with unreadable attachments turned into text. Never raises."""
    try:
        return await _prepare(params, service)
    except Exception as exc:
        logger.error(f"[file_bridge] skipped, request continues unchanged: {exc}")
        return params


async def _prepare(params: dict, service: str) -> dict:
    if not Config.FILE_BRIDGE_ENABLED or service == "deepgram" or params.get("type") == "image":
        return params
    configuration = params.get("configuration") or {}
    if configuration.get("file_bridge") is False:
        return params

    attachments = _current_attachments(params)
    history = configuration.get("conversation") or []
    has_history_files = any(isinstance(m, dict) and m.get("user_urls") for m in history)
    if not attachments and not has_history_files:
        return params

    params = dict(params)
    model = params.get("model")
    usage_log = params.get("file_bridge_usage")
    ctx = FileBridgeContext(
        org_id=params.get("org_id"),
        service_apikeys=params.get("service_apikeys") or {},
        usage_log=usage_log if isinstance(usage_log, list) else [],
    )
    for attachment in attachments:
        ctx.allowed_urls.add(attachment["url"])
        ctx.url_kinds[attachment["url"]] = attachment["kind"]
    params["original_attachments"] = [{"url": a["url"], "type": _saved_type(a)} for a in attachments]

    native = capabilities.native_kinds(service, model)
    to_convert = [a for a in attachments if a["kind"] not in native]
    budget = _request_budget(service, model)
    needs_tool = False

    if to_convert:
        semaphore = asyncio.Semaphore(CONVERT_CONCURRENCY)

        async def _run(attachment):
            async with semaphore:
                return await convert_url(attachment["url"], attachment["kind"], ctx)

        results = await asyncio.gather(*(_run(a) for a in to_convert))
        dropped = {a["url"] for a in to_convert}
        for bucket in ("images", "files", "audios"):
            if params.get(bucket):
                params[bucket] = [url for url in params[bucket] if url not in dropped]

        blocks = []
        for attachment, result in zip(to_convert, results, strict=True):
            kind = result.get("kind") or attachment["kind"]
            ctx.url_kinds[attachment["url"]] = kind
            block, truncated = _block(
                attachment["url"], kind, result.get("text") or "", max(min(part_size(), budget), 0), result.get("error")
            )
            budget -= len(block)
            needs_tool = needs_tool or truncated
            blocks.append(block)
        user = params.get("user") or ""
        params["user"] = "\n\n".join([user, *blocks]).strip()

    if has_history_files:
        new_history, budget, history_needs_tool = await _bridge_history(history, service, model, ctx, budget)
        needs_tool = needs_tool or history_needs_tool
        if new_history != history:
            params["configuration"] = {**configuration, "conversation": new_history}

    if needs_tool or READER_TOOL in (params.get("built_in_tools") or []):
        _add_reader_tool(params, service, model)
    params["file_bridge_ctx"] = ctx
    return params
