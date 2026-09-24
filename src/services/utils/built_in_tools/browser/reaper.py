"""Background loop that closes browser tabs whose conversation has gone quiet, on every host.

Steel never times sessions out on its own. Every gtwy pod runs this loop; a short
Redis lock makes sure only one of them acts per tick. Tabs are closed one by one.
The Steel session is never released: Steel restarts Chrome on release, which would
kill the tabs of every conversation that is still busy on that host.
"""

import asyncio
import time

import globals as _globals
from globals import logger
from src.services.cache_service import acquire_lock, release_lock

from . import cookies as cookie_store
from . import steel_client
from .connection import BrowserConnectionError, close_tab, get_browser, reset_connection
from .session_store import (
    acquire_registry_lock,
    clear_thread_state,
    cookie_meta,
    drop_tab,
    get_registry,
    release_registry_lock,
    save_registry,
    tab_idle_limit,
)
from .steel_client import SteelError
from .tabs import reconcile_registry

REAPER_LOCK = "gtwy_browser_reaper"
REAPER_LOCK_TTL = 55
REAPER_INTERVAL_SECONDS = 60


async def run_browser_reaper_loop() -> None:
    logger.info("Gtwy_Browser: tab reaper started")
    while _globals.is_ready:
        try:
            await reap_idle_tabs()
        except Exception as exc:
            logger.error(f"Gtwy_Browser: reaper tick failed: {exc.__class__.__name__}: {exc}")
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)
    logger.info("Gtwy_Browser: tab reaper stopped — server is shutting down")


async def reap_idle_tabs() -> None:
    if not steel_client.is_configured():
        return
    if not await acquire_lock(REAPER_LOCK, ttl=REAPER_LOCK_TTL):
        return
    try:
        for host in steel_client.hosts():
            try:
                await _reap_host(host)
            except Exception as exc:
                logger.error(f"Gtwy_Browser: reaper failed on {steel_client.short_host(host)}: {exc.__class__.__name__}: {exc}")
    finally:
        await release_lock(REAPER_LOCK)


async def _reap_host(host: str) -> None:
    if not await acquire_registry_lock(host):
        return
    try:
        registry = await get_registry(host)
        if not registry or not registry.get("tabs"):
            return

        # If Chrome was replaced since these tabs were recorded, they no longer exist and the
        # live links pointing at them would spin forever. Forget them now, without a tool call.
        try:
            reconciled = await reconcile_registry(host, registry, await steel_client.current_session(host))
        except SteelError as exc:
            logger.warning(f"Gtwy_Browser: reaper could not ask {steel_client.short_host(host)} for its session ({exc})")
            return
        if reconciled is not registry:
            await save_registry(host, reconciled)
            await reset_connection(host)
            return

        now = time.time()
        idle = [
            (key, tab)
            for key, tab in (registry.get("tabs") or {}).items()
            if now - float(tab.get("last_used_at") or 0) > tab_idle_limit(tab)
        ]
        if not idle:
            return

        try:
            browser = await get_browser(host, registry["steel_session_id"])
        except BrowserConnectionError as exc:
            # Chrome is still the one we know but is not answering right now. Leave the tabs
            # alone and try again next tick; they are not stale just because a connect failed.
            logger.warning(f"Gtwy_Browser: reaper cannot reach {steel_client.short_host(host)} ({exc}); will retry next tick")
            await reset_connection(host)
            return

        for key, tab in idle:
            await cookie_store.save_jar(host, browser, tab.get("browser_context_id"), key, cookie_meta(tab))
            await close_tab(host, browser, tab.get("target_id"), tab.get("browser_context_id"))
            drop_tab(registry, key)
            await clear_thread_state(key)
            logger.info(f"Gtwy_Browser: reaper closed idle tab of thread {key} on {steel_client.short_host(host)}")

        await save_registry(host, registry)
        if not registry.get("tabs"):
            # Nothing left to drive from this pod; drop the Playwright driver, keep Chrome running.
            await reset_connection(host)
            logger.info(f"Gtwy_Browser: no tabs left on {steel_client.short_host(host)}; Chrome stays up for the next conversation")
    finally:
        await release_registry_lock(host)
