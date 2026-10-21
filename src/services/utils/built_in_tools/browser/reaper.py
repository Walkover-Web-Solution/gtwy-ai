"""Background loop that stops the browsers of conversations that have gone quiet.

A Browser Use browser keeps running, and billing, until it is stopped or reaches its timeout;
disconnecting does not stop it. Every gtwy pod runs this loop; a short Redis lock makes sure
only one of them acts per tick.
"""

import asyncio

import globals as _globals
from globals import logger
from src.services.cache_service import acquire_lock, release_lock

from . import browser_use_client, browser_use_sessions

REAPER_LOCK = "gtwy_browser_reaper"
REAPER_LOCK_TTL = 55
REAPER_INTERVAL_SECONDS = 60


async def run_browser_reaper_loop() -> None:
    logger.info("Gtwy_Browser: browser reaper started")
    while _globals.is_ready:
        try:
            await reap_idle_tabs()
        except Exception as exc:
            logger.error(f"Gtwy_Browser: reaper tick failed: {exc.__class__.__name__}: {exc}")
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)
    logger.info("Gtwy_Browser: browser reaper stopped — server is shutting down")


async def reap_idle_tabs() -> None:
    if not browser_use_client.is_configured():
        return
    if not await acquire_lock(REAPER_LOCK, ttl=REAPER_LOCK_TTL):
        return
    try:
        await browser_use_sessions.reap_idle_browsers()
    finally:
        await release_lock(REAPER_LOCK)
