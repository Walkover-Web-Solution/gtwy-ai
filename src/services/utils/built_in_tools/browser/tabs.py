"""Assign one isolated browser tab per conversation and find it again on later turns."""

import time

from globals import logger

from . import cookies as cookie_store
from . import steel_client
from .connection import (
    BrowserConnectionError,
    close_tab,
    create_isolated_tab,
    find_page,
    get_browser,
)
from .session_store import (
    MAX_TABS,
    BrowserBusy,
    acquire_registry_lock,
    clear_thread_state,
    cookie_meta,
    drop_tab,
    empty_registry,
    evictable_tabs,
    get_registry,
    release_registry_lock,
    save_registry,
    seconds_until_a_tab_frees,
    touch_tab,
)
from .steel_client import SteelError


async def reconcile_registry(registry: dict, current: dict | None) -> dict:
    """Make the registry describe the Chrome Steel is running now.

    Chrome may have been replaced by something gtwy did not do: a person in the Steel UI,
    Steel itself, or another client. Every tab recorded for the old Chrome is gone, so they
    are forgotten here instead of being handed out as ghosts. Pure apart from clearing the
    per-thread Redis state, so it can be tested with a fake registry.
    """
    if current is None or registry.get("steel_session_id") == current["id"]:
        return registry
    tabs = list((registry.get("tabs") or {}).keys())
    if registry.get("steel_session_id"):
        logger.warning(
            f"Gtwy_Browser: Chrome was replaced (session {steel_client.redact_session_id(registry.get('steel_session_id'))}"
            f" -> {steel_client.redact_session_id(current['id'])}); {len(tabs)} recorded tab(s) are gone"
        )
    for tkey in tabs:
        await clear_thread_state(tkey)
    fresh = empty_registry()
    fresh.update(steel_session_id=current["id"], debug_url=current.get("debugUrl"))
    return fresh


async def _ensure_session_and_browser(registry: dict) -> tuple[dict, object]:
    """Return (registry, browser) for the Chrome Steel is running now.

    Never restarts Chrome because a connect failed: that used to kill every other
    conversation's tab and start the next failure. A failed connect is reported to the
    caller as BrowserConnectionError and the model is told to retry.
    """
    current = await steel_client.current_session()
    fresh = False
    if current is None:
        # Steel has no Chrome up at all (fresh container). This is the only time gtwy launches one.
        current = await steel_client.create_session()
        fresh = True
    registry = await reconcile_registry(registry, current)
    return registry, await get_browser(current["id"], fresh=fresh)


async def open_or_reuse_tab(tkey: str, org_id, bridge_id) -> tuple[object, object, dict, dict]:
    """Return (browser, page, registry, tab) for this conversation.

    Reuses the conversation's own tab when it still exists, otherwise opens a new one in
    its own cookie jar. Raises BrowserBusy when every tab belongs to an active conversation,
    and SteelError when the browser backend is unreachable.
    """
    if not await acquire_registry_lock():
        raise BrowserBusy(5, MAX_TABS)
    try:
        registry = await get_registry() or empty_registry()
        registry, browser = await _ensure_session_and_browser(registry)

        tab = (registry.get("tabs") or {}).get(tkey)
        if tab and tab.get("target_id"):
            page = await find_page(browser, tab["target_id"])
            if page is not None:
                tab = touch_tab(registry, tkey)
                await save_registry(registry)
                return browser, page, registry, tab
            # The tab was closed (reaper, crash, or Steel relaunching Chrome). Start a clean one.
            # Log the target id so a tab that vanishes mid-conversation can be traced in Steel's logs.
            logger.info(
                f"Gtwy_Browser: tab {tab['target_id'][-6:]} for thread {tkey} is gone; opening a new one"
            )
            drop_tab(registry, tkey)
            await clear_thread_state(tkey)

        open_tabs = len(registry.get("tabs") or {})
        if open_tabs >= MAX_TABS:
            for stale_key, stale_tab in evictable_tabs(registry, exclude=tkey):
                await cookie_store.save_jar(browser, stale_tab.get("browser_context_id"), stale_key, cookie_meta(stale_tab))
                await close_tab(browser, stale_tab.get("target_id"), stale_tab.get("browser_context_id"))
                drop_tab(registry, stale_key)
                await clear_thread_state(stale_key)
                logger.info(f"Gtwy_Browser: closed idle tab of thread {stale_key} to free a slot")
                if len(registry.get("tabs") or {}) < MAX_TABS:
                    break
            if len(registry.get("tabs") or {}) >= MAX_TABS:
                await save_registry(registry)
                raise BrowserBusy(seconds_until_a_tab_frees(registry, exclude=tkey), open_tabs)

        target_id, browser_context_id = await create_isolated_tab(browser)
        # Put this conversation's saved logins back so a returning user does not sign in again.
        await cookie_store.restore_jar(browser, browser_context_id, tkey)
        tab = touch_tab(
            registry,
            tkey,
            target_id=target_id,
            browser_context_id=browser_context_id,
            handoff_active=False,
            org_id=org_id,
            bridge_id=bridge_id,
            created_at=time.time(),
        )
        await save_registry(registry)
        page = await find_page(browser, target_id)
        if page is None:
            raise BrowserConnectionError("the new browser tab could not be opened")
        return browser, page, registry, tab
    finally:
        await release_registry_lock()


async def mark_handoff(tkey: str, active: bool) -> None:
    """Mirror the handoff flag onto the tab record so the reaper waits longer for a login."""
    if not await acquire_registry_lock():
        return
    try:
        registry = await get_registry()
        if registry and tkey in (registry.get("tabs") or {}):
            touch_tab(registry, tkey, handoff_active=active)
            await save_registry(registry)
    finally:
        await release_registry_lock()


async def release_tab(tkey: str, reason: str = "") -> None:
    """Close one conversation's tab and forget its state."""
    if not await acquire_registry_lock():
        return
    try:
        registry = await get_registry()
        if not registry:
            await clear_thread_state(tkey)
            return
        tab = drop_tab(registry, tkey)
        if tab:
            try:
                browser = await get_browser(registry["steel_session_id"])
                await cookie_store.save_jar(browser, tab.get("browser_context_id"), tkey, cookie_meta(tab))
                await close_tab(browser, tab.get("target_id"), tab.get("browser_context_id"))
            except (BrowserConnectionError, SteelError) as exc:
                logger.warning(f"Gtwy_Browser: could not close tab of {tkey}: {exc}")
            logger.info(f"Gtwy_Browser: released tab of thread {tkey} ({reason})")
        await clear_thread_state(tkey)
        # Never release the Steel session: Steel restarts Chrome on release and would take every
        # other conversation's tab with it. An empty tab list is a fine resting state.
        await save_registry(registry)
    finally:
        await release_registry_lock()
