"""Assign one isolated browser tab per conversation, on one host of the Steel pool, and find
it again on later turns.

A conversation stays on the host where its tab was opened. A new conversation goes to the
host with the fewest open tabs, and a host takes at most MAX_TABS_PER_HOST conversations, so
one heavy Chrome cannot starve the others (three real shopping pages already saturate a host
with the current 1.5 GB limit). When every host is full the caller gets BrowserBusy.
"""

import time
import zlib

from globals import logger

from . import cookies as cookie_store
from . import steel_client
from .connection import (
    BrowserConnectionError,
    close_tab,
    count_gtwy_tabs,
    create_isolated_tab,
    find_page,
    get_browser,
)
from .session_store import (
    MAX_TABS_PER_HOST,
    BrowserBusy,
    BrowserStarting,
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

# A host full of another environment's tabs gives no idle times to wait on, so the caller is
# told to come back after roughly one reaper pass.
FOREIGN_TABS_RETRY_SECONDS = 60


class HostFull(Exception):
    """A host has no room for another tab. ``own_tabs`` are the ones this environment can wait on."""

    def __init__(self, open_tabs: int, own_tabs: int):
        super().__init__(f"{open_tabs} tabs open")
        self.open_tabs = open_tabs
        self.own_tabs = own_tabs


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


async def _connect(host: str, registry: dict) -> tuple[dict | None, object | None]:
    """Return (session, browser) for the Chrome one host is running now, or (None, None) when
    the host has no Chrome up at all.

    Runs before the registry lock is taken: the Steel call and the CDP connect can take many
    seconds, and holding the lock through them made every other conversation waiting on this
    host give up. ``registry`` is an unlocked read, used only to judge whether Chrome is new.

    Never restarts Chrome because a connect failed: that used to kill every other
    conversation's tab and start the next failure. A failed connect is reported to the
    caller as BrowserConnectionError and the model is told to retry.
    """
    current = await steel_client.current_session(host)
    if current is None:
        return None, None
    # A session we have not seen before means Steel relaunched Chrome on its own (crash, OOM,
    # restart from its UI), so it may still be starting and deserves the fresh-connect retries.
    # No recorded id (first use, or the registry expired) says nothing about Chrome's age.
    fresh = registry.get("steel_session_id") not in (None, current["id"])
    return current, await get_browser(host, current["id"], fresh=fresh)


async def _launch(host: str) -> tuple[dict, object]:
    """Start Chrome on a host that has none (fresh container). Caller holds the registry lock,
    so two pods never both launch: a second launch would relaunch Chrome over the first."""
    current = await steel_client.current_session(host)
    if current is None:
        # The only time gtwy launches a Chrome.
        current = await steel_client.create_session(host)
    return current, await get_browser(host, current["id"], fresh=True)


def _placement_order(registries: dict[str, dict | None], tkey: str, exclude: str | None = None) -> list[str]:
    """Hosts with the fewest open tabs first.

    Ties are broken by a hash of the conversation and host, not by configured order: with
    every host empty, a burst of new conversations used to queue on the first host's lock
    and fail while the other hosts sat idle.
    """
    hosts = [h for h in registries if h != exclude]
    return sorted(hosts, key=lambda h: (open_tab_count(registries.get(h)), zlib.crc32(f"{tkey}|{h}".encode())))


async def _tabs_on_host(host: str, registry: dict, browser) -> int:
    """Tabs this host's Chrome is carrying: the larger of what Chrome reports (which includes
    other environments sharing the host) and this registry (which already counts a tab whose
    page is still appearing). Falls back to the registry when Chrome cannot be asked."""
    try:
        live = await count_gtwy_tabs(host, browser)
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: could not count tabs on {steel_client.short_host(host)}, using the registry ({exc.__class__.__name__})")
        live = 0
    return max(open_tab_count(registry), live)


async def _create_tab_on(host: str, registry: dict, browser, tkey: str, org_id, bridge_id):
    """Open this conversation's tab on a host whose registry lock the caller holds.

    Returns (page, registry, tab). Raises HostFull when the host is full even after closing
    this environment's idle tabs; tabs of other environments are counted but never closed.
    The registry is saved in both cases.
    """
    open_tabs = await _tabs_on_host(host, registry, browser)
    if open_tabs >= MAX_TABS_PER_HOST:
        for stale_key, stale_tab in evictable_tabs(registry, exclude=tkey):
            await cookie_store.save_jar(host, browser, stale_tab.get("browser_context_id"), stale_key, cookie_meta(stale_tab))
            await close_tab(host, browser, stale_tab.get("target_id"), stale_tab.get("browser_context_id"))
            drop_tab(registry, stale_key)
            await clear_thread_state(stale_key)
            logger.info(f"Gtwy_Browser: closed idle tab of thread {stale_key} on {steel_client.short_host(host)} to free a slot")
            open_tabs = await _tabs_on_host(host, registry, browser)
            if open_tabs < MAX_TABS_PER_HOST:
                break
        if open_tabs >= MAX_TABS_PER_HOST:
            await save_registry(host, registry)
            own = open_tab_count(registry)
            logger.info(
                f"Gtwy_Browser: {steel_client.short_host(host)} is full ({open_tabs} tabs, {own} from this environment)"
            )
            raise HostFull(open_tabs, own_tabs=own)

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
    """One attempt on one host: connect first, then take its registry lock for the bookkeeping.

    Returns (browser, page, registry, tab) on success, or None with ``reuse_only`` when the
    host no longer has this conversation's tab. Raises HostFull when the host has no room,
    BrowserStarting when another conversation holds the lock past the wait, and
    BrowserConnectionError/SteelError when the host cannot be reached.
    """
    current, browser = await _connect(host, await get_registry(host) or empty_registry(host))
    if not await acquire_registry_lock(host):
        raise BrowserStarting(5)
    try:
        registry = await get_registry(host) or empty_registry(host)
        if current is None:
            current, browser = await _launch(host)
        registry = await reconcile_registry(host, registry, current)

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

        page, registry, tab = await _create_tab_on(host, registry, browser, tkey, org_id, bridge_id)
        return browser, page, registry, tab
    finally:
        await release_registry_lock(host)


async def open_or_reuse_tab(tkey: str, org_id, bridge_id) -> tuple[object, object, dict, dict]:
    """Return (browser, page, registry, tab) for this conversation.

    Reuses the conversation's own tab on its host when it still exists, otherwise opens a new
    one in its own cookie jar on the least-loaded host with room. When no host gives a tab, the
    error names the most useful reason: BrowserBusy when a host was full of active
    conversations, BrowserStarting when hosts were only busy opening other conversations' tabs,
    and BrowserConnectionError when every host failed to connect.
    """
    hosts = steel_client.hosts()
    if not hosts:
        raise SteelError("no Steel host is configured")

    registries = await get_all_registries()
    home = find_tab_host(tkey, registries)
    order = ([home] if home else []) + _placement_order(registries, tkey, exclude=home)
    connection_errors: list[str] = []
    starting = False
    full_retry_in = None
    full_tabs = 0

    # The conversation's own host first (its tab, or a new tab there if it has room), then the
    # rest of the pool, least loaded first.
    for host in order:
        try:
            result = await _try_host(host, tkey, org_id, bridge_id, reuse_only=False)
        except HostFull as full:
            full_tabs += full.open_tabs
            if full.own_tabs:
                wait = seconds_until_a_tab_frees(registries.get(host) or {}, exclude=tkey)
            else:
                wait = FOREIGN_TABS_RETRY_SECONDS
            full_retry_in = min(full_retry_in or wait, wait)
            continue
        except BrowserStarting:
            logger.info(f"Gtwy_Browser: {steel_client.short_host(host)} busy opening another tab; thread {tkey} moves on")
            starting = True
            continue
        except (BrowserConnectionError, SteelError) as exc:
            connection_errors.append(str(exc))
            logger.warning(f"Gtwy_Browser: {steel_client.short_host(host)} unavailable for thread {tkey}: {exc}")
            continue
        if result is not None:
            return result

    if full_retry_in is not None:
        raise BrowserBusy(full_retry_in, full_tabs, len(hosts))
    if starting:
        raise BrowserStarting(5)
    raise BrowserConnectionError("; ".join(connection_errors)[:300])


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
