"""Playwright connection to the Steel browser over CDP, plus isolated tab management.

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

CONNECT_TIMEOUT_MS = 10_000
CONNECT_ATTEMPTS = 4
CONNECT_RETRY_SECONDS = 1.5
TAB_APPEAR_TIMEOUT_SECONDS = 10

_lock = asyncio.Lock()
_state = {"playwright": None, "browser": None, "session_id": None, "cdp": None, "pages": {}}


class BrowserConnectionError(Exception):
    """Could not reach or keep the CDP connection to Steel."""


async def _teardown_locked() -> None:
    playwright = _state.get("playwright")
    _state.update(playwright=None, browser=None, session_id=None, cdp=None, pages={})
    if playwright is not None:
        try:
            # Deliberately no browser.close(): that would dispose tabs inside Chrome.
            await playwright.stop()
        except Exception:
            pass


async def get_browser(steel_session_id: str):
    async with _lock:
        browser = _state.get("browser")
        if browser is not None and _state.get("session_id") == steel_session_id and browser.is_connected():
            return browser
        await _teardown_locked()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment problem
            raise BrowserConnectionError("playwright is not installed on this server") from exc

        playwright = await async_playwright().start()
        browser = None
        last_error = None
        # A session that was just created is still relaunching Chrome, so the first connect can
        # be refused. Retry briefly rather than failing the turn.
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    steel_client.cdp_ws_url(), timeout=CONNECT_TIMEOUT_MS
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < CONNECT_ATTEMPTS:
                    await asyncio.sleep(CONNECT_RETRY_SECONDS)
        if browser is None:
            await playwright.stop()
            detail = str(last_error).strip().splitlines()[0][:160] if last_error else "unknown error"
            raise BrowserConnectionError(f"could not connect to the browser: {detail}") from last_error

        _state.update(playwright=playwright, browser=browser, session_id=steel_session_id, cdp=None, pages={})
        logger.info(f"Gtwy_Browser: connected over CDP for session {steel_client.redact_session_id(steel_session_id)}")
        return browser


async def _browser_cdp(browser):
    cdp = _state.get("cdp")
    if cdp is None:
        cdp = await browser.new_browser_cdp_session()
        _state["cdp"] = cdp
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


async def _rescan(browser) -> dict:
    """Rebuild the target id to page map for this connection."""
    pages = {}
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            target_id = await _page_target_id(context, page)
            if target_id:
                pages[target_id] = page
    _state["pages"] = pages
    return pages


async def find_page(browser, target_id: str):
    """Return the page with this Chrome target id, or None if the tab is gone."""
    if not target_id:
        return None
    cached = _state.get("pages", {}).get(target_id)
    if cached is not None and not cached.is_closed():
        return cached
    return (await _rescan(browser)).get(target_id)


async def create_isolated_tab(browser) -> tuple[str, str]:
    """Open a tab in a brand new cookie jar. Returns (target_id, browser_context_id)."""
    cdp = await _browser_cdp(browser)
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
        if await find_page(browser, target_id) is not None:
            logger.info(f"Gtwy_Browser: opened isolated tab {target_id[-6:]}")
            return target_id, browser_context_id
        await asyncio.sleep(0.25)
    raise BrowserConnectionError("the new browser tab did not become available in time")


async def close_tab(browser, target_id: str | None, browser_context_id: str | None) -> None:
    """Close a tab and dispose its cookie jar. Errors are logged, never raised."""
    try:
        cdp = await _browser_cdp(browser)
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: no CDP session to close tab: {exc}")
        return
    if target_id:
        try:
            await cdp.send("Target.closeTarget", {"targetId": target_id})
        except Exception as exc:
            logger.warning(f"Gtwy_Browser: closing tab {target_id[-6:]} failed: {exc.__class__.__name__}")
        _state.get("pages", {}).pop(target_id, None)
    if browser_context_id:
        try:
            await cdp.send("Target.disposeBrowserContext", {"browserContextId": browser_context_id})
        except Exception as exc:
            logger.warning(f"Gtwy_Browser: disposing cookie jar failed: {exc.__class__.__name__}")


async def export_jar_cookies(browser, browser_context_id: str) -> list[dict]:
    """Every cookie in one tab's jar. Steel's own /context endpoint cannot see these jars."""
    cdp = await _browser_cdp(browser)
    result = await cdp.send("Storage.getCookies", {"browserContextId": browser_context_id})
    return result.get("cookies") or []


async def import_jar_cookies(browser, browser_context_id: str, cookies: list[dict]) -> int:
    """Load cookies into one tab's jar. Returns how many were sent."""
    if not cookies:
        return 0
    cdp = await _browser_cdp(browser)
    await cdp.send("Storage.setCookies", {"browserContextId": browser_context_id, "cookies": cookies})
    return len(cookies)


async def reset_connection() -> None:
    async with _lock:
        await _teardown_locked()


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
