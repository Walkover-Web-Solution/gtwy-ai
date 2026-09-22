"""Redis state for the browser tool: one registry of open tabs per Steel host, plus
per-conversation state.

``nd_gtwy_browser_registry_<host>`` is a JSON document describing that host's Chrome:
the Steel session it is running and one record per open tab, keyed by conversation::

    {
      "host": "https://api-1.browser.gtwy.ai",
      "steel_session_id": "...",
      "debug_url": "https://api-1.browser.gtwy.ai/v1/sessions/debug",
      "tabs": {
        "<org>:<thread>:<sub_thread>": {
          "host": "https://api-1.browser.gtwy.ai",
          "target_id": "...",            # Chrome target id, how any pod finds the tab
          "browser_context_id": "...",   # the tab's own cookie jar
          "last_used_at": 1788763000.0,
          "handoff_active": false
        }
      }
    }

Each conversation gets its own tab and its own logins on one host, and coming back to
an older conversation reuses that tab. ``nd_gtwy_browser_thread_<key>`` keeps the
volatile per-conversation state: the ref map, last URL, handoff flags and the host.
"""

import asyncio
import json
import time

from config import Config
from globals import logger
from src.configs.constant import redis_keys
from src.services.cache_service import acquire_lock, delete_in_cache, find_in_cache, release_lock, store_in_cache

from . import steel_client

REGISTRY_PREFIX = redis_keys["gtwy_browser_registry"]
THREAD_PREFIX = redis_keys["gtwy_browser_thread_"]
REGISTRY_LOCK_PREFIX = "gtwy_browser_registry"
REGISTRY_LOCK_TTL = 30
THREAD_STATE_TTL = 3600


class BrowserBusy(Exception):
    def __init__(self, retry_in: int, open_tabs: int, host_count: int = 1):
        where = f"across {host_count} browsers" if host_count > 1 else "in the browser"
        super().__init__(
            f"all {open_tabs} browser tabs {where} are in use by other conversations; retry in {retry_in} seconds"
        )
        self.retry_in = retry_in


# Close a tab after this much silence. A conversation mid-login gets longer, because the user
# typing into the live view never reaches gtwy, so the tab looks idle while they work.
IDLE_TIMEOUT_SECONDS = 300
HANDOFF_TIMEOUT_SECONDS = 900
# Conversations that can hold a tab at once inside one Steel host's Chrome. Measured on the
# current hosts (1.5 GB per container): three real shopping or login pages already put Steel at
# 80% memory and nearly two cores, and its live view stops streaming. Raise it only after the
# hosts get more memory and Steel's per-request logging is turned down.
MAX_TABS_PER_HOST = int(getattr(Config, "STEEL_MAX_TABS_PER_HOST", None) or 3)
# Safety net if the reaper dies; the reaper normally trims the registry long before this.
REGISTRY_TTL_SECONDS = max(IDLE_TIMEOUT_SECONDS, HANDOFF_TIMEOUT_SECONDS) * 6
# How long a caller waits for another conversation to finish its setup on the same host. Bounded
# by time, not by attempts: a slow Redis (SETNX has been seen taking 700ms) must not turn 40
# attempts into half a minute and eat the whole setup budget.
REGISTRY_LOCK_WAIT_SECONDS = 5.0


def tab_idle_limit(tab: dict) -> int:
    return HANDOFF_TIMEOUT_SECONDS if (tab or {}).get("handoff_active") else IDLE_TIMEOUT_SECONDS


def thread_key(org_id, thread_id, sub_thread_id) -> str:
    return f"{org_id}:{thread_id}:{sub_thread_id or thread_id}"


def _thread_redis_key(tkey: str) -> str:
    return f"{THREAD_PREFIX}{tkey}"


def _registry_key(host: str) -> str:
    return f"{REGISTRY_PREFIX}_{steel_client.host_slug(host)}"


def _registry_lock_name(host: str) -> str:
    return f"{REGISTRY_LOCK_PREFIX}_{steel_client.host_slug(host)}"


async def _load(key: str) -> dict | None:
    raw = await find_in_cache(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- registry, one per host


def empty_registry(host: str) -> dict:
    return {"host": host, "steel_session_id": None, "debug_url": None, "created_at": time.time(), "tabs": {}}


async def get_registry(host: str) -> dict | None:
    return await _load(_registry_key(host))


async def get_all_registries() -> dict[str, dict | None]:
    """Every configured host's registry, None where a host has none yet."""
    out: dict[str, dict | None] = {}
    for host in steel_client.hosts():
        out[host] = await get_registry(host)
    return out


def find_tab_host(tkey: str, registries: dict[str, dict | None]) -> str | None:
    """The host whose registry records a tab for this conversation, if any."""
    for host, registry in registries.items():
        if tkey in ((registry or {}).get("tabs") or {}):
            return host
    return None


def open_tab_count(registry: dict | None) -> int:
    return len((registry or {}).get("tabs") or {})


async def save_registry(host: str, registry: dict) -> None:
    registry["host"] = host
    await store_in_cache(_registry_key(host), registry, ttl=REGISTRY_TTL_SECONDS)


async def clear_registry(host: str) -> None:
    await delete_in_cache(_registry_key(host))


async def acquire_registry_lock(host: str) -> bool:
    deadline = asyncio.get_event_loop().time() + REGISTRY_LOCK_WAIT_SECONDS
    while True:
        if await acquire_lock(_registry_lock_name(host), ttl=REGISTRY_LOCK_TTL):
            return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


async def release_registry_lock(host: str) -> None:
    await release_lock(_registry_lock_name(host))


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
        "host": None,
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
