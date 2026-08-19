"""Optional, opt-in downscaling/re-encoding of user-supplied images.

Large images (multi-MB phone photos) either stall the provider — which downloads
the URL itself — or bloat the base64 payload we send. When the caller opts in via
the ``compress_images`` request key, images above the configured thresholds are
downscaled and re-encoded before they reach the model.

Compression is best-effort: any failure falls back to the original bytes so it can
never turn a working completion into a failed one.
"""

import asyncio
import base64
from io import BytesIO

from PIL import Image, ImageOps

from globals import logger

from .apiservice import fetch, fetch_images_b64

DEFAULT_MAX_DIMENSION = 2048
DEFAULT_QUALITY = 80
DEFAULT_MAX_BYTES = 3 * 1024 * 1024  # 3 MB

# Formats Pillow can re-encode. Anything else (svg, tiff variants, ...) passes through.
_ENCODABLE = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


def normalize_compress_options(raw):
    """Accept ``True`` / ``False`` / ``None`` / dict and return options or ``None``.

    ``None`` means "compression disabled", so callers can branch cheaply without
    re-deriving the defaults.
    """
    if not raw:
        return None
    if raw is True:
        options = {}
    elif isinstance(raw, dict):
        if raw.get("enabled") is False:
            return None
        options = raw
    else:
        return None

    def _positive_int(key, default):
        try:
            value = int(options.get(key) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    quality = _positive_int("quality", DEFAULT_QUALITY)
    return {
        "max_dimension": _positive_int("max_dimension", DEFAULT_MAX_DIMENSION),
        "quality": min(quality, 95),
        "max_bytes": _positive_int("max_bytes", DEFAULT_MAX_BYTES),
    }


def compress_image_bytes(raw_bytes, mime, options):
    """Return ``(bytes, mime)`` — re-encoded when oversized, otherwise unchanged.

    Synchronous and CPU-bound; call it via ``asyncio.to_thread`` from async code.
    """
    if not options or not raw_bytes:
        return raw_bytes, mime

    base_mime = (mime or "").split(";")[0].strip().lower()
    if base_mime and base_mime not in _ENCODABLE:
        return raw_bytes, mime

    max_dimension = options["max_dimension"]
    try:
        with Image.open(BytesIO(raw_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            oversized = max(width, height) > max_dimension
            if not oversized and len(raw_bytes) <= options["max_bytes"]:
                return raw_bytes, mime

            if oversized:
                image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            buffer = BytesIO()
            if has_alpha:
                image.convert("RGBA").save(buffer, format="WEBP", quality=options["quality"])
                out_mime = "image/webp"
            else:
                image.convert("RGB").save(
                    buffer, format="JPEG", quality=options["quality"], optimize=True
                )
                out_mime = "image/jpeg"
            compressed = buffer.getvalue()
    except Exception as e:  # noqa: BLE001 - never fail a completion over compression
        logger.warning(f"compress_image_bytes: skipping compression ({type(e).__name__}: {e})")
        return raw_bytes, mime

    if len(compressed) >= len(raw_bytes):
        # Re-encoding made it bigger (already well-optimized source) — keep the original.
        return raw_bytes, mime

    logger.info(
        f"compress_image_bytes: {len(raw_bytes)} -> {len(compressed)} bytes ({base_mime or '?'} -> {out_mime})"
    )
    return compressed, out_mime


async def fetch_images_b64_compressed(urls, options=None):
    """Download images and return ``[(base64, mime), ...]``, compressing when enabled.

    Mirrors :func:`src.services.utils.apiservice.fetch_images_b64`; delegates to it
    verbatim when compression is disabled so existing behavior is byte-identical.
    """
    if not urls:
        return []
    if not options:
        return list(await fetch_images_b64(urls))

    async def _one(url):
        image, headers = await fetch(url, image=True)
        raw_bytes = image.getvalue()
        mime = headers.get("Content-Type")
        compressed, out_mime = await asyncio.to_thread(
            compress_image_bytes, raw_bytes, mime, options
        )
        return base64.b64encode(compressed).decode("utf-8"), out_mime

    return list(await asyncio.gather(*(_one(url) for url in urls)))


def to_data_urls(pairs):
    """``[(base64, mime), ...]`` -> ``["data:<mime>;base64,<b64>", ...]``."""
    return [f"data:{mime or 'image/jpeg'};base64,{b64}" for b64, mime in pairs]


async def fetch_images_as_data_urls(urls, options=None):
    """Convenience for providers that take an ``image_url`` string."""
    return to_data_urls(await fetch_images_b64_compressed(urls, options))
