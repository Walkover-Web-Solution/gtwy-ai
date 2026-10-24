"""Per-conversation browser state in Redis, and the idle limits shared by the session code.

``nd_gtwy_browser_thread_<org>:<thread>:<sub_thread>`` keeps what the model's next call needs:
the ref map of the last snapshot and the handoff flags. The conversation's
browser itself is recorded by ``sessions.py``.
"""

import json

from globals import logger
from src.configs.constant import redis_keys
from src.services.cache_service import delete_in_cache, find_in_cache, store_in_cache

THREAD_PREFIX = redis_keys["gtwy_browser_thread_"]
THREAD_STATE_TTL = 3600


class BrowserBusy(Exception):
    """No browser can be started right now; the caller is told when to retry."""

    def __init__(self, message: str, retry_in: int):
        super().__init__(message)
        self.retry_in = retry_in


class BrowserStarting(BrowserBusy):
    """Another call of the same conversation is starting its browser right now."""

    def __init__(self, retry_in: int):
        super().__init__(f"the browser for this conversation is starting; retry in {retry_in} seconds", retry_in)


# Stop a browser after this much silence. A conversation mid-login gets longer, because the user
# typing into the live view never reaches gtwy, so the browser looks idle while they work.
IDLE_TIMEOUT_SECONDS = 300
HANDOFF_TIMEOUT_SECONDS = 900
# Safety net for a conversation's browser record if the reaper dies; the reaper normally
# removes it long before this.
RECORD_TTL_SECONDS = max(IDLE_TIMEOUT_SECONDS, HANDOFF_TIMEOUT_SECONDS) * 6


def tab_idle_limit(record: dict) -> int:
    return HANDOFF_TIMEOUT_SECONDS if (record or {}).get("handoff_active") else IDLE_TIMEOUT_SECONDS


def thread_key(org_id, thread_id, sub_thread_id) -> str:
    return f"{org_id}:{thread_id}:{sub_thread_id or thread_id}"


def _thread_redis_key(tkey: str) -> str:
    return f"{THREAD_PREFIX}{tkey}"


async def _load(key: str) -> dict | None:
    raw = await find_in_cache(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def default_thread_state() -> dict:
    return {
        "ref_map": {},
        "handoff_active": False,
        "handoff_message": None,
        "handoff_message_id": None,
    }


async def get_thread_state(tkey: str) -> dict:
    state = await _load(_thread_redis_key(tkey))
    return {**default_thread_state(), **(state or {})}


async def save_thread_state(tkey: str, state: dict) -> None:
    await store_in_cache(_thread_redis_key(tkey), state, ttl=THREAD_STATE_TTL)


async def clear_thread_state(tkey: str | None) -> None:
    if tkey:
        await delete_in_cache(_thread_redis_key(tkey))
        logger.info(f"Gtwy_Browser: cleared browser state for thread {tkey}")
