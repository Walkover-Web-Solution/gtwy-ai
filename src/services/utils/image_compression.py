"""Downscale oversized images before they are sent to a model.

Large user-shared images (multi-MB phone photos) made calls time out: providers
that were handed a bare URL had to download the file themselves, and providers
handed base64 got a payload inflated ~33% on top of the original size.

Images are downloaded once, downscaled/re-encoded when they exceed the
thresholds below, and returned as base64 so the provider never fetches anything.
Images already within the thresholds are returned untouched.

Compression is best-effort: any failure falls back to the original bytes, so it
can never turn a working completion into a failed one.
"""

import asyncio
import base64
from io import BytesIO

from PIL import Image, ImageOps

from globals import logger

from .apiservice import fetch

# Roughly the largest a vision model actually benefits from; also keeps the
# base64 payload well inside provider request limits.
MAX_DIMENSION = 2048
QUALITY = 80
MAX_BYTES = 3 * 1024 * 1024  # leave anything already under 3 MB alone

# Formats Pillow can re-encode. Anything else (svg, exotic tiff, ...) passes through.
_ENCODABLE = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


def compress_image_bytes(raw_bytes, mime):
    """Return ``(bytes, mime)`` — re-encoded when oversized, otherwise unchanged.

    Synchronous and CPU-bound; call it via ``asyncio.to_thread`` from async code.
    """
    if not raw_bytes:
        return raw_bytes, mime

    base_mime = (mime or "").split(";")[0].strip().lower()
    if base_mime and base_mime not in _ENCODABLE:
        return raw_bytes, mime

    try:
        with Image.open(BytesIO(raw_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            oversized = max(width, height) > MAX_DIMENSION
            if not oversized and len(raw_bytes) <= MAX_BYTES:
                return raw_bytes, mime

            if oversized:
                image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            buffer = BytesIO()
            if has_alpha:
                image.convert("RGBA").save(buffer, format="WEBP", quality=QUALITY)
                out_mime = "image/webp"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=QUALITY, optimize=True)
                out_mime = "image/jpeg"
            compressed = buffer.getvalue()
    except Exception as e:  # noqa: BLE001 - never fail a completion over compression
        logger.warning(f"compress_image_bytes: skipping compression ({type(e).__name__}: {e})")
        return raw_bytes, mime

    if len(compressed) >= len(raw_bytes):
        # Re-encoding grew it (already well-optimized source) — keep the original.
        return raw_bytes, mime

    logger.info(
        f"compress_image_bytes: {len(raw_bytes)} -> {len(compressed)} bytes "
        f"({base_mime or '?'} -> {out_mime})"
    )
    return compressed, out_mime


async def fetch_images_b64(urls):
    """Download images and return ``[(base64, mime), ...]``, compressing oversized ones.

    Drop-in replacement for :func:`src.services.utils.apiservice.fetch_images_b64`.
    """
    if not urls:
        return []

    async def _one(url):
        image, headers = await fetch(url, image=True)
        compressed, mime = await asyncio.to_thread(
            compress_image_bytes, image.getvalue(), headers.get("Content-Type")
        )
        return base64.b64encode(compressed).decode("utf-8"), mime

    return list(await asyncio.gather(*(_one(url) for url in urls)))


def to_data_urls(pairs):
    """``[(base64, mime), ...]`` -> ``["data:<mime>;base64,<b64>", ...]``."""
    return [f"data:{mime or 'image/jpeg'};base64,{b64}" for b64, mime in pairs]


async def fetch_images_as_data_urls(urls):
    """Convenience for providers that take an ``image_url`` string."""
    return to_data_urls(await fetch_images_b64(urls))
