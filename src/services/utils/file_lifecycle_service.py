"""Resolve user file URLs to provider Files-API file_ids (dedup + registry).

Contract: every failure path returns None for that URL and the chat request
falls back to today's URL/base64 behavior — this feature is additive, never
load-bearing. See docs/file_lifecycle_design.md.
"""

import asyncio
import hashlib
import ipaddress
import json
import mimetypes
import socket

import aiohttp
import certifi
import ssl
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

from globals import logger
from src.configs.constant import file_lifecycle_config, redis_keys
from src.db_services import provider_files_service as registry
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.provider_file_adapters import get_file_adapter

_ssl_context = ssl.create_default_context(cafile=certifi.where())

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=15)
_CHUNK_SIZE = 64 * 1024
_UPLOAD_RACE_POLLS = 2
_UPLOAD_RACE_POLL_DELAY = 0.5


def _allowlisted_hosts() -> set[str]:
    raw = file_lifecycle_config["download_host_allowlist"] or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_forbidden_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class _SafeResolver(AbstractResolver):
    """DNS resolver that rejects private/internal IPs on every connection.

    Because validation happens at resolve time for each connection, redirects
    and DNS rebinding both go through the same check.
    """

    def __init__(self, allow_hosts: set[str]):
        self._resolver = ThreadedResolver()
        self._allow_hosts = allow_hosts

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self._resolver.resolve(host, port, family)
        if host.lower() in self._allow_hosts:
            return results
        for entry in results:
            if _is_forbidden_ip(entry["host"]):
                raise aiohttp.ClientConnectionError(
                    f"Blocked download from non-public address for host {host}"
                )
        return results

    async def close(self):
        await self._resolver.close()


async def safe_download(url: str, max_bytes: int) -> tuple[str, bytes, str] | None:
    """SSRF-safe streaming download. Returns (filename, data, mime_type) or None."""
    if not url or not url.lower().startswith("https://"):
        logger.error(f"file_lifecycle: rejected non-https url: {str(url)[:120]}")
        return None
    try:
        connector = aiohttp.TCPConnector(ssl=_ssl_context, resolver=_SafeResolver(_allowlisted_hosts()))
        async with aiohttp.ClientSession(connector=connector, timeout=_DOWNLOAD_TIMEOUT) as session:
            async with session.get(url, max_redirects=3, allow_redirects=True) as resp:
                if resp.status >= 300:
                    logger.error(f"file_lifecycle: download failed HTTP {resp.status} for {url[:120]}")
                    return None
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    logger.error(f"file_lifecycle: file too large ({declared} bytes) at {url[:120]}")
                    return None
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    total += len(chunk)
                    if total > max_bytes:
                        logger.error(f"file_lifecycle: download exceeded {max_bytes} bytes, aborted: {url[:120]}")
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                filename = url.split("?")[0].rstrip("/").split("/")[-1] or "input_file"
                mime_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                if not mime_type or mime_type == "application/octet-stream":
                    guessed, _ = mimetypes.guess_type(filename)
                    mime_type = guessed or mime_type or "application/octet-stream"
                return filename, data, mime_type
    except Exception as e:
        logger.error(f"file_lifecycle: download error for {str(url)[:120]}: {e}")
        return None


def _cache_id(provider: str, apikey_object_id: str, sha256: str) -> str:
    return f"{redis_keys['provider_file_']}{provider}:{apikey_object_id}:{sha256}"


async def resolve_file(
    url: str,
    *,
    provider: str,
    apikey: str,
    apikey_object_id: str,
    org_id: str,
    thread_id: str | None,
) -> dict | None:
    """Resolve one URL to {"file_id", "mime_type"} on the given provider, or None."""
    adapter = get_file_adapter(provider)
    if adapter is None or not apikey or not apikey_object_id:
        return None

    downloaded = await safe_download(url, min(file_lifecycle_config["max_download_bytes"], adapter.max_size_bytes))
    if downloaded is None:
        return None
    filename, data, mime_type = downloaded
    sha256 = hashlib.sha256(data).hexdigest()
    apikey_object_id = str(apikey_object_id)

    # 1. Redis dedup cache
    cache_id = _cache_id(provider, apikey_object_id, sha256)
    cached = await find_in_cache(cache_id)
    if cached:
        try:
            entry = json.loads(cached)
            asyncio.create_task(registry.touch_usage_by_key(provider, apikey_object_id, sha256, thread_id))
            return {"file_id": entry["file_id"], "mime_type": entry.get("mime_type") or mime_type}
        except Exception:
            pass  # malformed cache entry — fall through to Mongo

    # 2. Mongo registry (source of truth)
    existing = await registry.find_reusable(provider, apikey_object_id, sha256)
    if existing is None:
        # 3. Register intent, then upload (crash-safe ordering)
        intent_id = await registry.insert_upload_intent(
            provider=provider,
            apikey_object_id=apikey_object_id,
            org_id=org_id,
            content_sha256=sha256,
            source_url=url,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            purpose=adapter.upload_purpose,
            thread_id=thread_id,
        )
        if intent_id is None:
            # lost an insert race — the winner is uploading
            existing = await registry.find_reusable(provider, apikey_object_id, sha256)
        else:
            try:
                file_id = await adapter.upload(apikey, filename, data, mime_type)
            except Exception as e:
                logger.error(f"file_lifecycle: {provider} upload failed for {filename}: {e}")
                await registry.delete_row(intent_id)
                return None
            await registry.mark_active(intent_id, file_id)
            await store_in_cache(cache_id, {"file_id": file_id, "mime_type": mime_type}, ttl=file_lifecycle_config["ttl_seconds"])
            return {"file_id": file_id, "mime_type": mime_type}

    # 4. Reuse an existing row (found directly or after losing the race)
    for _ in range(_UPLOAD_RACE_POLLS + 1):
        if existing is None:
            return None
        if existing.get("status") == registry.FILE_STATUS["active"] and existing.get("file_id"):
            await registry.touch_usage(existing["_id"], thread_id)
            await store_in_cache(
                cache_id,
                {"file_id": existing["file_id"], "mime_type": existing.get("mime_type") or mime_type},
                ttl=file_lifecycle_config["ttl_seconds"],
            )
            return {"file_id": existing["file_id"], "mime_type": existing.get("mime_type") or mime_type}
        # still uploading in another request — brief poll, then give up (URL fallback)
        await asyncio.sleep(_UPLOAD_RACE_POLL_DELAY)
        existing = await registry.find_reusable(provider, apikey_object_id, sha256)
    return None


async def resolve_files(
    urls: list[str],
    *,
    provider: str,
    apikey: str,
    apikey_object_id: str,
    org_id: str,
    thread_id: str | None,
) -> dict[str, dict]:
    """Resolve many URLs concurrently. Returns {url: {"file_id", "mime_type"}} for successes only."""
    if not urls:
        return {}
    results = await asyncio.gather(
        *(
            resolve_file(
                url,
                provider=provider,
                apikey=apikey,
                apikey_object_id=apikey_object_id,
                org_id=org_id,
                thread_id=thread_id,
            )
            for url in urls
        ),
        return_exceptions=True,
    )
    resolved = {}
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            logger.error(f"file_lifecycle: resolve failed for {str(url)[:120]}: {result}")
        elif result:
            resolved[url] = result
    return resolved
