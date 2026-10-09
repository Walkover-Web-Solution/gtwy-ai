import asyncio
import hashlib
import json

from src.configs.constant import redis_keys
from src.services.cache_service import acquire_lock, find_in_cache, release_lock, store_in_cache

LOCK_TTL_SECONDS = 300
LOCK_WAIT_SECONDS = 60
LOCK_POLL_SECONDS = 1


def cache_key(url: str) -> str:
    return f"{redis_keys['file_text_']}{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


async def get_cached(url: str) -> dict | None:
    raw = await find_in_cache(cache_key(url))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and "text" in value else None


async def set_cached(url: str, value: dict) -> None:
    await store_in_cache(cache_key(url), value)


async def get_or_convert(url: str, convert):
    """Return the cached conversion of ``url``, or run ``convert()`` once and cache it.

    A lock stops two requests paying for the same helper-model call at once;
    the loser waits for the winner's result instead.
    """
    cached = await get_cached(url)
    if cached:
        return cached

    key = cache_key(url)
    locked = await acquire_lock(key, ttl=LOCK_TTL_SECONDS)
    if not locked:
        for _ in range(int(LOCK_WAIT_SECONDS / LOCK_POLL_SECONDS)):
            await asyncio.sleep(LOCK_POLL_SECONDS)
            cached = await get_cached(url)
            if cached:
                return cached
        # The other request is stuck or failed; convert it ourselves.
    try:
        value = await convert()
        if value and not value.get("error"):
            await set_cached(url, value)
        return value
    finally:
        if locked:
            await release_lock(key)
