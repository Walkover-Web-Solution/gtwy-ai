import asyncio
import base64
import ssl
from io import BytesIO

import aiohttp
import certifi

from globals import logger

# Created once at startup — avoids reading CA bundle from disk on every request
_ssl_context = ssl.create_default_context(cafile=certifi.where())

_GATEWAY_ERROR_CODES = (502, 520)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt: 1s → 2s → 4s

# Lazy import to break the circular chain:
#   apiservice → send_alert → alert_utils → apiservice
async def _fire_gateway_alert(status_code, url, error_text):
    try:
        from src.services.commonServices.baseService.utils import unknown_error_handler_alert
        await unknown_error_handler_alert({
            "error_log": {"status_code": status_code, "url": url, "error": error_text[:500]},
            "service":"openai",
            "is_external_error":False
            })
    except Exception as e:
        logger.error(f"_fire_gateway_alert failed: {e}")


async def fetch(url, method="GET", headers=None, params=None, json_body=None, image=None):
    timeout = aiohttp.ClientTimeout(total=600, connect=15)
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        connector = aiohttp.TCPConnector(ssl=_ssl_context)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            body = None if method.upper() == "GET" else json_body
            async with session.request(
                method=method, url=url, headers=headers, params=params, json=body
            ) as response:
                if response.status >= 300:
                    error_response = await response.text()
                    if response.status in _GATEWAY_ERROR_CODES:
                        asyncio.create_task(_fire_gateway_alert(response.status, url, error_response))
                        if attempt < _MAX_RETRIES:
                            delay = _RETRY_BASE_DELAY * (2 ** attempt)
                            logger.warning(
                                f"fetch: HTTP {response.status} from {url}, "
                                f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_MAX_RETRIES})"
                            )
                            last_exc = ValueError(error_response)
                            await asyncio.sleep(delay)
                            continue
                    raise ValueError(error_response)
                if image:
                    response_data = BytesIO(await response.read())
                else:
                    response_data = await response.json()
                response_headers = dict(response.headers)
                return response_data, response_headers
    raise last_exc


async def _fetch_stream_once(url, method, headers, json_body):
    """Single streaming attempt; raises ValueError (with status in message) on bad status."""
    timeout = aiohttp.ClientTimeout(total=900, connect=15, sock_read=120)
    connector = aiohttp.TCPConnector(ssl=_ssl_context)
    async with aiohttp.ClientSession(
        connector=connector,
        read_bufsize=64 * 1024 * 1024,
        timeout=timeout,
    ) as session:
        async with session.request(method=method, url=url, headers=headers, json=json_body) as response:
            if response.status >= 300:
                error_response = await response.text()
                if response.status in _GATEWAY_ERROR_CODES:
                    asyncio.create_task(_fire_gateway_alert(response.status, url, error_response))
                raise ValueError(f"HTTP {response.status}: {error_response}")
            async for line in response.content:
                decoded = line.decode("utf-8").strip()
                if decoded:
                    yield decoded


async def fetch_stream(url, method="POST", headers=None, json_body=None):
    """Async generator that yields raw SSE lines from a streaming HTTP response."""
    # 10 MiB buffer — large enough for SSE lines that embed base64 payloads
    # (e.g. OpenAI ``response.image_generation_call.partial_image``), which
    # exceed aiohttp's default ~128 KB per-line limit.
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for line in _fetch_stream_once(url, method, headers, json_body):
                yield line
            return
        except ValueError as e:
            error_str = str(e)
            is_gateway_error = any(f"HTTP {code}:" in error_str for code in _GATEWAY_ERROR_CODES)
            if is_gateway_error and attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"fetch_stream: gateway error from {url}, "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_MAX_RETRIES})"
                )
                last_exc = e
                await asyncio.sleep(delay)
                continue
            raise
    if last_exc:
        raise last_exc


async def fetch_images_b64(urls):
    if not urls:
        return []
    images_res, headers = zip(*await asyncio.gather(*(fetch(url, image=True) for url in urls)), strict=False)
    images_data = [base64.b64encode(image.getvalue()).decode("utf-8") for image in images_res]
    images_media_type = [header.get("Content-Type") for header in headers]
    return zip(images_data, images_media_type, strict=False)
