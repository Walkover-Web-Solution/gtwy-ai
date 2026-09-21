"""Assign one isolated browser tab per conversation, on one host of the Steel pool, and find
it again on later turns.

A conversation stays on the host where its tab was opened. A new conversation goes to the
host with the fewest open tabs, and a host takes at most MAX_TABS_PER_HOST conversations, so
one heavy Chrome cannot starve the others (three real shopping pages already saturate a host
with the current 1.5 GB limit). When every host is full the caller gets BrowserBusy.
"""

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
    reset_connection,
)
from .frozen_tabs import close_frozen_tabs
from .session_store import (
    MAX_TABS_PER_HOST,
    BrowserBusy,
    acquire_registry_lock,
    clear_thread_state,
    cookie_meta,
    drop_tab,
    empty_registry,
    evictable_tabs,
    find_tab_host,
    get_all_registries,
    get_registry,
    open_tab_count,
    release_registry_lock,
    save_registry,
    seconds_until_a_tab_frees,
    touch_tab,
)
from .steel_client import SteelError


async def reconcile_registry(host: str, registry: dict, current: dict | None) -> dict:
    """Make one host's registry describe the Chrome that host is running now.

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
            f"Gtwy_Browser: Chrome on {steel_client.short_host(host)} was replaced (session "
            f"{steel_client.redact_session_id(registry.get('steel_session_id'))} -> "
            f"{steel_client.redact_session_id(current['id'])}); {len(tabs)} recorded tab(s) are gone"
        )
    for tkey in tabs:
        await clear_thread_state(tkey)
    fresh = empty_registry(host)
    fresh.update(steel_session_id=current["id"], debug_url=current.get("debugUrl"))
    return fresh


async def forget_tabs(host: str, registry: dict, target_ids: list[str]) -> list[str]:
    """Drop the registry records of tabs that no longer exist. Returns the affected thread keys."""
    gone = [key for key, tab in (registry.get("tabs") or {}).items() if tab.get("target_id") in target_ids]
    for key in gone:
        drop_tab(registry, key)
        await clear_thread_state(key)
        logger.warning(f"Gtwy_Browser: thread {key} lost its frozen tab on {steel_client.short_host(host)}; it gets a new one on its next call")
    return gone


async def _connect_or_repair(host: str, registry: dict, session_id: str, fresh: bool):
    """Connect to a host's Chrome; if the handshake hangs, close frozen tabs and try once more.

    A connect that times out while Chrome's browser process still answers means one tab's
    renderer is frozen and is blocking every new connection. Closing that tab, and only that
    tab, unblocks everyone else. Any other failure is reported as is, never with a restart.
    """
    try:
        return await get_browser(host, session_id, fresh=fresh)
    except BrowserConnectionError as exc:
        if not exc.timed_out:
            raise
        logger.warning(f"Gtwy_Browser: connect to {steel_client.short_host(host)} hung; looking for frozen tabs")
        closed = await close_frozen_tabs(host)
        if not closed:
            raise
        await forget_tabs(host, registry, closed)
        await save_registry(host, registry)
        await reset_connection(host)
        return await get_browser(host, session_id, fresh=fresh)


async def _ensure_session_and_browser(host: str, registry: dict) -> tuple[dict, object]:
    """Return (registry, browser) for the Chrome one host is running now.

    Never restarts Chrome because a connect failed: that used to kill every other
    conversation's tab and start the next failure. A hung connect gets the frozen-tab repair;
    any other failed connect is reported to the caller as BrowserConnectionError and the
    model is told to retry.
    """
    current = await steel_client.current_session(host)
    fresh = False
    if current is None:
        # The host has no Chrome up at all (fresh container). This is the only time gtwy launches one.
        current = await steel_client.create_session(host)
        fresh = True
    registry = await reconcile_registry(host, registry, current)
    return registry, await _connect_or_repair(host, registry, current["id"], fresh)


def _placement_order(registries: dict[str, dict | None], exclude: str | None = None) -> list[str]:
    """Hosts with the fewest open tabs first, in configured order among equals."""
    hosts = [h for h in registries if h != exclude]
    return sorted(hosts, key=lambda h: open_tab_count(registries.get(h)))


async def _create_tab_on(host: str, registry: dict, browser, tkey: str, org_id, bridge_id):
    """Open this conversation's tab on a host whose registry lock the caller holds.

    Returns (page, registry, tab), or None when the host is full even after closing its
    idle tabs. The registry is saved in both cases.
    """
    if open_tab_count(registry) >= MAX_TABS_PER_HOST:
        for stale_key, stale_tab in evictable_tabs(registry, exclude=tkey):
            await cookie_store.save_jar(host, browser, stale_tab.get("browser_context_id"), stale_key, cookie_meta(stale_tab))
            await close_tab(host, browser, stale_tab.get("target_id"), stale_tab.get("browser_context_id"))
            drop_tab(registry, stale_key)
            await clear_thread_state(stale_key)
            logger.info(f"Gtwy_Browser: closed idle tab of thread {stale_key} on {steel_client.short_host(host)} to free a slot")
            if open_tab_count(registry) < MAX_TABS_PER_HOST:
                break
        if open_tab_count(registry) >= MAX_TABS_PER_HOST:
            await save_registry(host, registry)
            return None

    target_id, browser_context_id = await create_isolated_tab(host, browser)
    # Put this conversation's saved logins back so a returning user does not sign in again.
    # Cookies are stored per conversation, not per host, so they follow it to any Chrome.
    await cookie_store.restore_jar(host, browser, browser_context_id, tkey)
    tab = touch_tab(
        registry,
        tkey,
        host=host,
        target_id=target_id,
        browser_context_id=browser_context_id,
        handoff_active=False,
        org_id=org_id,
        bridge_id=bridge_id,
        created_at=time.time(),
    )
    await save_registry(host, registry)
    page = await find_page(host, browser, target_id)
    if page is None:
        raise BrowserConnectionError("the new browser tab could not be opened")
    return page, registry, tab


async def _try_host(host: str, tkey: str, org_id, bridge_id, reuse_only: bool):
    """One attempt on one host, under its registry lock.

    Returns (browser, page, registry, tab) on success, None when the host is full (or, with
    ``reuse_only``, when it no longer has this conversation's tab), and raises
    BrowserConnectionError/SteelError when the host cannot be reached.
    """
    if not await acquire_registry_lock(host):
        raise BrowserBusy(5, MAX_TABS_PER_HOST, 1)
    try:
        registry = await get_registry(host) or empty_registry(host)
        registry, browser = await _ensure_session_and_browser(host, registry)

        tab = (registry.get("tabs") or {}).get(tkey)
        if tab and tab.get("target_id"):
            page = await find_page(host, browser, tab["target_id"])
            if page is not None:
                tab = touch_tab(registry, tkey, host=host)
                await save_registry(host, registry)
                return browser, page, registry, tab
            # The tab was closed (reaper, crash, or Steel relaunching Chrome). Start a clean one.
            # Log the target id so a tab that vanishes mid-conversation can be traced in Steel's logs.
            logger.info(
                f"Gtwy_Browser: tab {tab['target_id'][-6:]} for thread {tkey} on {steel_client.short_host(host)} is gone; opening a new one"
            )
            drop_tab(registry, tkey)
            await clear_thread_state(tkey)
        elif reuse_only:
            await save_registry(host, registry)
            return None

        created = await _create_tab_on(host, registry, browser, tkey, org_id, bridge_id)
        if created is None:
            return None
        page, registry, tab = created
        return browser, page, registry, tab
    finally:
        await release_registry_lock(host)


async def open_or_reuse_tab(tkey: str, org_id, bridge_id) -> tuple[object, object, dict, dict]:
    """Return (browser, page, registry, tab) for this conversation.

    Reuses the conversation's own tab on its host when it still exists, otherwise opens a new
    one in its own cookie jar on the least-loaded host with room. Raises BrowserBusy when every
    host is full of active conversations, SteelError when no host can be reached over REST and
    BrowserConnectionError when the CDP connection to every candidate host failed.
    """
    hosts = steel_client.hosts()
    if not hosts:
        raise SteelError("no Steel host is configured")

    registries = await get_all_registries()
    home = find_tab_host(tkey, registries)
    tried: list[str] = []
    connection_errors: list[str] = []
    retry_in = None
    open_total = sum(open_tab_count(r) for r in registries.values())

    # The conversation's own host first: its tab, or a new tab there if it has room.
    if home:
        tried.append(home)
        try:
            result = await _try_host(home, tkey, org_id, bridge_id, reuse_only=False)
            if result is not None:
                return result
            retry_in = seconds_until_a_tab_frees(registries.get(home) or {}, exclude=tkey)
        except (BrowserConnectionError, SteelError) as exc:
            connection_errors.append(str(exc))
            logger.warning(f"Gtwy_Browser: {steel_client.short_host(home)} unavailable for thread {tkey}: {exc}")

    # Then the rest of the pool, least loaded first.
    for host in _placement_order(registries, exclude=home):
        tried.append(host)
        try:
            result = await _try_host(host, tkey, org_id, bridge_id, reuse_only=False)
        except BrowserBusy as busy:
            retry_in = min(retry_in or busy.retry_in, busy.retry_in)
            continue
        except (BrowserConnectionError, SteelError) as exc:
            connection_errors.append(str(exc))
            logger.warning(f"Gtwy_Browser: {steel_client.short_host(host)} unavailable for thread {tkey}: {exc}")
            continue
        if result is not None:
            return result
        wait = seconds_until_a_tab_frees(registries.get(host) or {}, exclude=tkey)
        retry_in = min(retry_in or wait, wait)

    if connection_errors and len(connection_errors) == len(tried):
        raise BrowserConnectionError("; ".join(connection_errors)[:300])
    raise BrowserBusy(retry_in or 5, open_total, len(hosts))


async def _host_of(tkey: str) -> str | None:
    return find_tab_host(tkey, await get_all_registries())


async def mark_handoff(tkey: str, active: bool) -> None:
    """Mirror the handoff flag onto the tab record so the reaper waits longer for a login."""
    host = await _host_of(tkey)
    if not host or not await acquire_registry_lock(host):
        return
    try:
        registry = await get_registry(host)
        if registry and tkey in (registry.get("tabs") or {}):
            touch_tab(registry, tkey, handoff_active=active)
            await save_registry(host, registry)
    finally:
        await release_registry_lock(host)


async def release_tab(tkey: str, reason: str = "") -> None:
    """Close one conversation's tab and forget its state."""
    host = await _host_of(tkey)
    if not host:
        await clear_thread_state(tkey)
        return
    if not await acquire_registry_lock(host):
        return
    try:
        registry = await get_registry(host)
        if not registry:
            await clear_thread_state(tkey)
            return
        tab = drop_tab(registry, tkey)
        if tab:
            try:
                browser = await get_browser(host, registry["steel_session_id"])
                await cookie_store.save_jar(host, browser, tab.get("browser_context_id"), tkey, cookie_meta(tab))
                await close_tab(host, browser, tab.get("target_id"), tab.get("browser_context_id"))
            except (BrowserConnectionError, SteelError) as exc:
                logger.warning(f"Gtwy_Browser: could not close tab of {tkey} on {steel_client.short_host(host)}: {exc}")
            logger.info(f"Gtwy_Browser: released tab of thread {tkey} on {steel_client.short_host(host)} ({reason})")
        await clear_thread_state(tkey)
        # Never release the Steel session: Steel restarts Chrome on release and would take every
        # other conversation's tab with it. An empty tab list is a fine resting state.
        await save_registry(host, registry)
    finally:
        await release_registry_lock(host)
