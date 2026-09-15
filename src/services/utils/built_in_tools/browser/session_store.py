"""Redis state for the browser tool: one registry of open tabs, plus per-conversation state.

``nd_gtwy_browser_registry`` is a single JSON document describing the shared Chrome:
the Steel session it belongs to and one record per open tab, keyed by conversation::

    {
      "steel_session_id": "...",
      "debug_url": "http://steel/v1/sessions/debug",
      "tabs": {
        "<org>:<thread>:<sub_thread>": {
          "target_id": "...",            # Chrome target id, how any pod finds the tab
          "browser_context_id": "...",   # the tab's own cookie jar
          "last_used_at": 1788763000.0,
          "handoff_active": false
        }
      }
    }

Each conversation therefore gets its own tab and its own logins, and coming back to
an older conversation reuses that conversation's tab. ``nd_gtwy_browser_thread_<key>``
keeps the volatile per-conversation state: the ref map, last URL and handoff flags.
"""

import asyncio
import json
import time

from globals import logger
from src.configs.constant import redis_keys
from src.services.cache_service import acquire_lock, delete_in_cache, find_in_cache, release_lock, store_in_cache

REGISTRY_KEY = redis_keys["gtwy_browser_registry"]
THREAD_PREFIX = redis_keys["gtwy_browser_thread_"]
REGISTRY_LOCK = "gtwy_browser_registry"
REGISTRY_LOCK_TTL = 30
THREAD_STATE_TTL = 3600


class BrowserBusy(Exception):
    def __init__(self, retry_in: int, open_tabs: int):
        super().__init__(
            f"all {open_tabs} browser tabs are in use by other conversations; retry in {retry_in} seconds"
        )
        self.retry_in = retry_in


# Close a tab after this much silence. A conversation mid-login gets longer, because the user
# typing into the live view never reaches gtwy, so the tab looks idle while they work.
IDLE_TIMEOUT_SECONDS = 300
HANDOFF_TIMEOUT_SECONDS = 900
# Conversations that can hold a tab at once inside one Steel container.
MAX_TABS = 10
# Safety net if the reaper dies; the reaper normally clears the registry long before this.
REGISTRY_TTL_SECONDS = max(IDLE_TIMEOUT_SECONDS, HANDOFF_TIMEOUT_SECONDS) * 6


def tab_idle_limit(tab: dict) -> int:
    return HANDOFF_TIMEOUT_SECONDS if (tab or {}).get("handoff_active") else IDLE_TIMEOUT_SECONDS


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


# ---------------------------------------------------------------- registry


def empty_registry() -> dict:
    return {"steel_session_id": None, "debug_url": None, "created_at": time.time(), "tabs": {}}


async def get_registry() -> dict | None:
    return await _load(REGISTRY_KEY)


async def save_registry(registry: dict) -> None:
    await store_in_cache(REGISTRY_KEY, registry, ttl=REGISTRY_TTL_SECONDS)


async def clear_registry() -> None:
    await delete_in_cache(REGISTRY_KEY)


async def acquire_registry_lock() -> bool:
    for _ in range(40):
        if await acquire_lock(REGISTRY_LOCK, ttl=REGISTRY_LOCK_TTL):
            return True
        await asyncio.sleep(0.1)
    return False


async def release_registry_lock() -> None:
    await release_lock(REGISTRY_LOCK)


def touch_tab(registry: dict, tkey: str, **updates) -> dict:
    tab = registry.setdefault("tabs", {}).setdefault(tkey, {})
    tab["last_used_at"] = time.time()
    tab.update(updates)
    return tab


def cookie_meta(tab: dict | None) -> dict:
    """Fields worth keeping alongside a stored cookie blob, for support queries."""
    return {"org_id": (tab or {}).get("org_id"), "bridge_id": (tab or {}).get("bridge_id")}


def evictable_tabs(registry: dict, exclude: str) -> list[tuple[str, dict]]:
    """Tabs of other conversations that have been idle past their limit, oldest first."""
    now = time.time()
    stale = [
        (key, tab)
        for key, tab in (registry.get("tabs") or {}).items()
        if key != exclude and now - float(tab.get("last_used_at") or 0) > tab_idle_limit(tab)
    ]
    return sorted(stale, key=lambda item: float(item[1].get("last_used_at") or 0))


def seconds_until_a_tab_frees(registry: dict, exclude: str) -> int:
    now = time.time()
    waits = [
        tab_idle_limit(tab) - (now - float(tab.get("last_used_at") or 0))
        for key, tab in (registry.get("tabs") or {}).items()
        if key != exclude
    ]
    return max(1, int(min(waits))) if waits else 1


def drop_tab(registry: dict, tkey: str) -> dict | None:
    return (registry.get("tabs") or {}).pop(tkey, None)


# ---------------------------------------------------------------- per-thread state


def default_thread_state() -> dict:
    return {
        "target_id": None,
        "last_url": None,
        "snapshot_seq": 0,
        "ref_map": {},
        "handoff_active": False,
        "handoff_message": None,
        "handoff_message_id": None,
        "handoff_started_at": None,
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
