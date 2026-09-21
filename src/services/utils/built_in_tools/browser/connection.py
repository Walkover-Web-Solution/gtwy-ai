"""Playwright connections to the Steel hosts over CDP, plus isolated tab management.

One connection per host per pod. Every function takes the host's base URL first, so a pod
can drive several Chromes at once without their state mixing.

Design notes learned from probing Steel's Chrome:

* Tabs are created with raw CDP ``Target.createBrowserContext`` +
  ``Target.createTarget`` rather than Playwright's ``new_context()``. Raw contexts
  are their own cookie jar, they survive a disconnect, and a fresh process can
  find them again by target id. Playwright's own contexts are destroyed when the
  connection closes, which would kill a user's login.
* ``browser.close()`` is never called: over CDP it tears down contexts. We only
  stop the Playwright driver, leaving Chrome and every tab untouched.
* Playwright reports every tab under ``browser.contexts[0]`` regardless of its real
  cookie jar, so a sibling tab for the same jar must also go through raw CDP.
"""

import asyncio

from globals import logger

from . import steel_client

# The whole setup is capped at SETUP_TIMEOUT_SECONDS (25s) in tool.py. Connecting to a Chrome we
# already know gets one attempt: when it hangs, a second attempt only burns the budget, and the
# caller reports "try again" rather than restarting Chrome (a restart kills every conversation's
# tab). A Chrome that Steel just launched is still starting, so its attempts are shorter and retried.
CONNECT_TIMEOUT_MS = 7_000
FRESH_CONNECT_TIMEOUT_MS = 5_000
FRESH_CONNECT_ATTEMPTS = 2
CONNECT_RETRY_SECONDS = 1.0
# A failure this quick was a refusal or a network error, not a frozen Chrome, so one retry is cheap.
QUICK_FAILURE_SECONDS = 2.0
TAB_APPEAR_TIMEOUT_SECONDS = 6

_states: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


def _state(host: str) -> dict:
    return _states.setdefault(host, {"playwright": None, "browser": None, "session_id": None, "cdp": None, "pages": {}})


def _lock(host: str) -> asyncio.Lock:
    return _locks.setdefault(host, asyncio.Lock())


class BrowserConnectionError(Exception):
    """Could not reach or keep the CDP connection to a Steel host.

    ``timed_out`` is True when the websocket opened but Chrome never finished the handshake,
    which is what a single frozen tab looks like from the outside.
    """

    def __init__(self, message: str, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


async def _teardown_locked(host: str) -> None:
    state = _state(host)
    playwright = state.get("playwright")
    state.update(playwright=None, browser=None, session_id=None, cdp=None, pages={})
    if playwright is not None:
        try:
            # Deliberately no browser.close(): that would dispose tabs inside Chrome.
            await playwright.stop()
        except Exception:
            pass


def should_retry_connect(attempt: int, elapsed_seconds: float, fresh: bool) -> bool:
    """Whether another connect attempt is worth its time.

    A Chrome that was just launched (``fresh``) may refuse the first attempt while it starts, so
    it gets FRESH_CONNECT_ATTEMPTS. A Chrome we already know gets a retry only after a quick
    failure such as a refusal; when the attempt ran to its timeout, waiting again is pointless
    and the caller should give up for this call and ask for a retry.
    """
    if fresh:
        return attempt + 1 < FRESH_CONNECT_ATTEMPTS
    return attempt == 0 and elapsed_seconds < QUICK_FAILURE_SECONDS


async def get_browser(host: str, steel_session_id: str, fresh: bool = False):
    """Connect to one host's Chrome, reusing the live connection when it is for this session.

    ``fresh`` says the session was created a moment ago, so Chrome may still be starting.
    """
    async with _lock(host):
        state = _state(host)
        browser = state.get("browser")
        if browser is not None and state.get("session_id") == steel_session_id and browser.is_connected():
            return browser
        await _teardown_locked(host)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment problem
            raise BrowserConnectionError("playwright is not installed on this server") from exc

        playwright = await async_playwright().start()
        browser = None
        last_error = None
        attempt = 0
        started = asyncio.get_event_loop().time()
        timeout_ms = FRESH_CONNECT_TIMEOUT_MS if fresh else CONNECT_TIMEOUT_MS
        while browser is None:
            attempt_started = asyncio.get_event_loop().time()
            try:
                browser = await playwright.chromium.connect_over_cdp(steel_client.cdp_ws_url(host), timeout=timeout_ms)
            except Exception as exc:
                last_error = exc
                elapsed = asyncio.get_event_loop().time() - attempt_started
                if not should_retry_connect(attempt, elapsed, fresh):
                    break
                attempt += 1
                await asyncio.sleep(CONNECT_RETRY_SECONDS)
        if browser is None:
            await playwright.stop()
            detail = str(last_error).strip().splitlines()[0][:160] if last_error else "unknown error"
            raise BrowserConnectionError(
                f"could not connect to {steel_client.short_host(host)}: {detail}",
                timed_out=is_timeout_error(last_error) if last_error else False,
            ) from last_error

        state.update(playwright=playwright, browser=browser, session_id=steel_session_id, cdp=None, pages={})
        logger.info(
            f"Gtwy_Browser: connected over CDP to {steel_client.short_host(host)} for session "
            f"{steel_client.redact_session_id(steel_session_id)} in {asyncio.get_event_loop().time() - started:.1f}s"
        )
        return browser


async def _browser_cdp(host: str, browser):
    state = _state(host)
    cdp = state.get("cdp")
    if cdp is None or state.get("browser") is not browser:
        cdp = await browser.new_browser_cdp_session()
        if state.get("browser") is browser:
            state["cdp"] = cdp
    return cdp


async def _page_target_id(context, page) -> str | None:
    try:
        session = await context.new_cdp_session(page)
    except Exception:
        return None
    try:
        info = await session.send("Target.getTargetInfo")
        return info["targetInfo"]["targetId"]
    except Exception:
        return None
    finally:
        try:
            await session.detach()
        except Exception:
            pass


async def _rescan(host: str, browser) -> dict:
    """Rebuild the target id to page map for this host's connection."""
    pages = {}
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            target_id = await _page_target_id(context, page)
            if target_id:
                pages[target_id] = page
    _state(host)["pages"] = pages
    return pages


async def find_page(host: str, browser, target_id: str):
    """Return the page with this Chrome target id, or None if the tab is gone."""
    if not target_id:
        return None
    cached = _state(host).get("pages", {}).get(target_id)
    if cached is not None and not cached.is_closed():
        return cached
    return (await _rescan(host, browser)).get(target_id)


async def create_isolated_tab(host: str, browser) -> tuple[str, str]:
    """Open a tab in a brand new cookie jar. Returns (target_id, browser_context_id)."""
    cdp = await _browser_cdp(host, browser)
    try:
        created = await cdp.send("Target.createBrowserContext", {"disposeOnDetach": False})
        browser_context_id = created["browserContextId"]
        target = await cdp.send(
            "Target.createTarget", {"url": "about:blank", "browserContextId": browser_context_id}
        )
        target_id = target["targetId"]
        # Refuse downloads in this tab. Some sites answer a normal-looking URL with a file, which
        # would otherwise abort the navigation and leave the agent stuck on that page.
        try:
            await cdp.send(
                "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": browser_context_id}
            )
        except Exception as exc:
            logger.warning(f"Gtwy_Browser: could not disable downloads: {exc.__class__.__name__}")
    except Exception as exc:
        raise BrowserConnectionError(
            f"could not open a browser tab: {str(exc).strip().splitlines()[0][:160] if str(exc).strip() else exc.__class__.__name__}"
        ) from exc

    deadline = asyncio.get_event_loop().time() + TAB_APPEAR_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if await find_page(host, browser, target_id) is not None:
            logger.info(f"Gtwy_Browser: opened isolated tab {target_id[-6:]} on {steel_client.short_host(host)}")
            return target_id, browser_context_id
        await asyncio.sleep(0.25)
    raise BrowserConnectionError("the new browser tab did not become available in time")


async def close_tab(host: str, browser, target_id: str | None, browser_context_id: str | None) -> None:
    """Close a tab and dispose its cookie jar. Errors are logged, never raised."""
    try:
        cdp = await _browser_cdp(host, browser)
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: no CDP session to close tab: {exc}")
        return
    if target_id:
        try:
            await cdp.send("Target.closeTarget", {"targetId": target_id})
        except Exception as exc:
            logger.warning(f"Gtwy_Browser: closing tab {target_id[-6:]} failed: {exc.__class__.__name__}")
        _state(host).get("pages", {}).pop(target_id, None)
    if browser_context_id:
        try:
            await cdp.send("Target.disposeBrowserContext", {"browserContextId": browser_context_id})
        except Exception as exc:
            logger.warning(f"Gtwy_Browser: disposing cookie jar failed: {exc.__class__.__name__}")


async def export_jar_cookies(host: str, browser, browser_context_id: str) -> list[dict]:
    """Every cookie in one tab's jar. Steel's own /context endpoint cannot see these jars."""
    cdp = await _browser_cdp(host, browser)
    result = await cdp.send("Storage.getCookies", {"browserContextId": browser_context_id})
    return result.get("cookies") or []


async def import_jar_cookies(host: str, browser, browser_context_id: str, cookies: list[dict]) -> int:
    """Load cookies into one tab's jar. Returns how many were sent."""
    if not cookies:
        return 0
    cdp = await _browser_cdp(host, browser)
    await cdp.send("Storage.setCookies", {"browserContextId": browser_context_id, "cookies": cookies})
    return len(cookies)


async def reset_connection(host: str | None = None) -> None:
    """Drop the Playwright driver for one host, or for every host when none is given."""
    for h in ([host] if host else list(_states.keys())):
        async with _lock(h):
            await _teardown_locked(h)


def is_connection_lost_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    text = str(exc)
    return (
        name in ("TargetClosedError", "BrowserConnectionError")
        or "Target closed" in text
        or "Connection closed" in text
        or "has been closed" in text
        or "browser has been closed" in text.lower()
    )


def is_timeout_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "TimeoutError" or "Timeout" in str(exc)[:60]
