"""This pod's Playwright connections to the conversations' Browser Use browsers.

Every pod keeps one Playwright driver and one CDP connection per browser it has touched. Any
pod can drive any conversation's browser: two clients on one browser work side by side. When
Browser Use stops a browser its CDP socket closes and the connection forgets itself.

Design notes learned from probing Browser Use browsers:

* The browser's default context is used, never ``new_context()``: the default context is
  where the conversation's profile (its saved logins) is loaded, and a fresh context starts
  logged out.
* ``browser.close()`` on a CDP connection also closes what Playwright opened in it, so it is
  only called for a browser that is stopped or lost. Disconnecting never stops the browser.
"""

import asyncio

from globals import logger

from .browser_use_client import redact

# Browser Use is on the public internet, so a connect takes 2-5 s; anything near this is a hang.
CONNECT_TIMEOUT_MS = 12_000
# A browser that was just started may refuse its first connect while Chrome comes up.
FRESH_CONNECT_ATTEMPTS = 3
FRESH_CONNECT_RETRY_SECONDS = 1.0

_playwright = None
_playwright_lock = asyncio.Lock()
_browsers: dict[str, object] = {}
_connect_locks: dict[str, asyncio.Lock] = {}


class BrowserConnectionError(Exception):
    """Could not reach or keep the CDP connection to a conversation's browser."""


async def _driver():
    global _playwright
    async with _playwright_lock:
        if _playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - environment problem
                raise BrowserConnectionError("playwright is not installed on this server") from exc
            _playwright = await async_playwright().start()
        return _playwright


async def connect(browser_id: str, cdp_url: str, fresh: bool = False):
    """This pod's connection to one browser, opened on first use and reused while it lives.

    ``fresh`` says the browser was started a moment ago, so a refused connect is retried.
    """
    async with _connect_locks.setdefault(browser_id, asyncio.Lock()):
        browser = _browsers.get(browser_id)
        if browser is not None and browser.is_connected():
            return browser
        _browsers.pop(browser_id, None)
        playwright = await _driver()
        attempts = FRESH_CONNECT_ATTEMPTS if fresh else 1
        last_error = None
        started = asyncio.get_event_loop().time()
        for attempt in range(attempts):
            try:
                browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=CONNECT_TIMEOUT_MS)
                break
            except Exception as exc:
                last_error = exc
                browser = None
                if attempt + 1 < attempts:
                    await asyncio.sleep(FRESH_CONNECT_RETRY_SECONDS)
        if browser is None:
            detail = str(last_error).strip().splitlines()[0][:160] if last_error else "unknown error"
            raise BrowserConnectionError(f"could not connect to browser {redact(browser_id)}: {detail}") from last_error

        def _forget_connection(closed, browser_id=browser_id):
            if _browsers.get(browser_id) is closed:
                _browsers.pop(browser_id, None)

        browser.on("disconnected", _forget_connection)
        _browsers[browser_id] = browser
        logger.info(
            f"Gtwy_Browser: connected over CDP to browser {redact(browser_id)} "
            f"in {asyncio.get_event_loop().time() - started:.1f}s"
        )
        return browser


async def disconnect(browser_id: str | None) -> None:
    """Forget this pod's connection to one browser. Only for a browser that is stopped or lost."""
    browser = _browsers.pop(browser_id, None) if browser_id else None
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass


async def browser_cdp_send(browser, method: str, params: dict | None = None) -> dict:
    """One browser-level CDP command (it acts on the default context, where the profile lives)."""
    cdp = await browser.new_browser_cdp_session()
    try:
        return await cdp.send(method, params or {})
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


async def deny_downloads(browser) -> None:
    """Some sites answer a normal-looking URL with a file, which aborts the navigation and
    leaves the agent stuck on that page."""
    try:
        await browser_cdp_send(browser, "Browser.setDownloadBehavior", {"behavior": "deny"})
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: could not disable downloads: {exc.__class__.__name__}")


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


async def target_id_of(page) -> str | None:
    """The Chrome target id of one page, which is how the conversation remembers its tab."""
    return await _page_target_id(page.context, page)


async def conversation_page(browser, wanted_target_id: str | None):
    """(page, target_id) for the conversation: its last page if still open, else the first open
    page of the default context, else a new one there."""
    if not browser.contexts:
        raise BrowserConnectionError("the browser has no default context")
    context = browser.contexts[0]
    first = None
    for page in context.pages:
        if page.is_closed():
            continue
        target_id = await _page_target_id(context, page)
        if wanted_target_id and target_id == wanted_target_id:
            return page, target_id
        if first is None and target_id:
            first = (page, target_id)
    if first is not None:
        return first
    page = await context.new_page()
    return page, await _page_target_id(context, page)


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
