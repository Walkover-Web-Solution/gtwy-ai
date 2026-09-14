"""Background loop that closes browser tabs whose conversation has gone quiet.

Steel never times sessions out on its own. Every gtwy pod runs this loop; a short
Redis lock makes sure only one of them acts per tick. Tabs are closed one by one,
and the whole Steel session is released once no tab is left.
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
    clear_registry,
    clear_thread_state,
    cookie_meta,
    drop_tab,
    get_registry,
    release_registry_lock,
    save_registry,
    tab_idle_limit,
)

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
        if not await acquire_registry_lock():
            return
        try:
            registry = await get_registry()
            if not registry or not registry.get("steel_session_id"):
                return

            now = time.time()
            idle = [
                (key, tab)
                for key, tab in (registry.get("tabs") or {}).items()
                if now - float(tab.get("last_used_at") or 0) > tab_idle_limit(tab)
            ]
            if not idle and registry.get("tabs"):
                return

            browser = None
            if idle:
                try:
                    browser = await get_browser(registry["steel_session_id"])
                except BrowserConnectionError as exc:
                    logger.warning(f"Gtwy_Browser: reaper cannot reach the browser ({exc}); clearing registry")
                    for key in list((registry.get("tabs") or {}).keys()):
                        await clear_thread_state(key)
                    await clear_registry()
                    await reset_connection()
                    return

            for key, tab in idle:
                await cookie_store.save_jar(browser, tab.get("browser_context_id"), key, cookie_meta(tab))
                await close_tab(browser, tab.get("target_id"), tab.get("browser_context_id"))
                drop_tab(registry, key)
                await clear_thread_state(key)
                logger.info(f"Gtwy_Browser: reaper closed idle tab of thread {key}")

            if registry.get("tabs"):
                await save_registry(registry)
            else:
                await steel_client.release_session(registry.get("steel_session_id"))
                await clear_registry()
                await reset_connection()
                logger.info("Gtwy_Browser: no tabs left; released the Steel session")
        finally:
            await release_registry_lock()
    finally:
        await release_lock(REAPER_LOCK)
