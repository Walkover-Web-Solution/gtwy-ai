"""Thin REST client for Browser Use Cloud browsers (https://docs.browser-use.com, API v4).

Only the browser infrastructure is used: ``POST /browsers`` starts a hosted Chromium (stealth
on by default) and returns its CDP URL and live view URL, and gtwy drives it with Playwright.
The Agent API (``/runs``) is not used: the model of the gtwy agent keeps deciding every click.

Each browser is its own Chromium, so one conversation's browser can never show another
conversation's pages. A browser runs until it is stopped or its ``timeout`` (at most 240
minutes) passes; disconnecting CDP does not stop it, so the reaper stops idle ones.
Both URLs a browser returns are bearer credentials: anyone holding them can control it.
"""

import re

import httpx

from config import Config
from globals import logger

HTTP_TIMEOUT = 20.0
COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}$")
API_URL = "https://api.browser-use.com/api/v4"
# Hard lifetime of one browser; Browser Use allows at most 240 minutes. The reaper stops idle
# browsers long before this, so it only matters for a conversation that stays busy.
BROWSER_TIMEOUT_MINUTES = 240
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
# Every browser goes through Browser Use's residential proxy in this country unless the agent's
# browser settings pick another one, or turn it off with proxy_country "none". The proxy gets
# past much more bot protection (Myntra only loads through it) but costs about $4.9 per GB.
DEFAULT_PROXY_COUNTRY = "in"
PROXY_OFF_VALUES = {"none", "off", "false", "no"}


class BrowserUseError(Exception):
    """A Browser Use REST call failed or the service is unreachable."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TooManyBrowsers(BrowserUseError):
    """The account is at its limit of browsers running at once."""


class ProfileNotFound(BrowserUseError):
    """The profile id given to a new browser does not exist (deleted on Browser Use's side)."""


def is_configured() -> bool:
    return bool(getattr(Config, "BROWSER_USE_API_KEY", None))


def redact(value: str | None) -> str:
    """Browser and profile ids in logs: enough to match lines, not a full id."""
    if not value:
        return "<none>"
    return f"...{str(value)[-6:]}"


async def _request(method: str, path: str, json_body: dict | None = None, params=None) -> dict | None:
    headers = {"X-Browser-Use-API-Key": Config.BROWSER_USE_API_KEY or ""}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.request(method, f"{API_URL}{path}", json=json_body, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise BrowserUseError(f"browser use unreachable: {exc.__class__.__name__}: {str(exc)[:120]}") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str((response.json() or {}).get("detail") or "")[:200]
        except ValueError:
            detail = response.text[:200]
        message = f"browser use {method} {path.split('/')[1] if '/' in path else path} returned {response.status_code}: {detail}"
        if response.status_code == 429:
            raise TooManyBrowsers(message, response.status_code)
        if response.status_code == 404 and "profile" in detail.lower():
            raise ProfileNotFound(message, response.status_code)
        raise BrowserUseError(message, response.status_code)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def proxy_country(value) -> str | None:
    """The proxy country for an agent's ``proxy_country`` setting, or None for no proxy.

    Not set (or not a two-letter code) means DEFAULT_PROXY_COUNTRY; "none" turns the proxy off.
    """
    country = str(value if value is not None else "").strip().lower()
    if country in PROXY_OFF_VALUES or value is False:
        return None
    return country if COUNTRY_CODE_RE.match(country) else DEFAULT_PROXY_COUNTRY


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    """Start one hosted browser. Returns its view: id, status, cdpUrl, liveUrl, timeoutAt...

    ``country`` is the proxy country already chosen with ``proxy_country()``; None means no proxy.
    """
    body = {
        "timeout": BROWSER_TIMEOUT_MINUTES,
        "browserScreenWidth": DEFAULT_VIEWPORT["width"],
        "browserScreenHeight": DEFAULT_VIEWPORT["height"],
        "proxyCountryCode": country,
    }
    if profile_id:
        body["profileId"] = profile_id
    if metadata:
        body["metadata"] = {str(k): str(v) for k, v in metadata.items() if v is not None}
    data = await _request("POST", "/browsers", body)
    if not data or not data.get("id") or not data.get("cdpUrl"):
        raise BrowserUseError("browser use returned no browser id or cdp url")
    logger.info(
        f"Gtwy_Browser: started browser use browser {redact(data['id'])} "
        f"(profile {redact(profile_id)}, proxy {body['proxyCountryCode'] or 'none'}, timeout {body['timeout']} min)"
    )
    return data


async def get_browser(browser_id: str) -> dict | None:
    """The browser's current view, or None when Browser Use does not know it."""
    try:
        return await _request("GET", f"/browsers/{browser_id}")
    except BrowserUseError as exc:
        if exc.status == 404:
            return None
        raise


async def stop_browser(browser_id: str) -> dict | None:
    """Stop a browser for good. Its profile, if any, is saved when it stops."""
    try:
        data = await _request("PATCH", f"/browsers/{browser_id}", {"action": "stop"})
    except BrowserUseError as exc:
        if exc.status == 404:
            return None
        raise
    logger.info(f"Gtwy_Browser: stopped browser use browser {redact(browser_id)}")
    return data


async def list_active_browsers(metadata_terms: list[str] | None = None, page_size: int = 100) -> list[dict]:
    """Running browsers, optionally only those tagged with every ``key=value`` term."""
    params: list[tuple[str, str | int]] = [("filterBy", "active"), ("pageSize", page_size)]
    for term in metadata_terms or []:
        params.append(("metadata", term))
    data = await _request("GET", "/browsers", params=params)
    return list((data or {}).get("items") or [])


async def create_profile(name: str, user_id: str | None = None) -> str:
    """Create an empty profile and return its id."""
    body = {"name": name[:200]}
    if user_id:
        body["userId"] = str(user_id)
    data = await _request("POST", "/profiles", body)
    if not data or not data.get("id"):
        raise BrowserUseError("browser use returned no profile id")
    logger.info(f"Gtwy_Browser: created browser use profile {redact(data['id'])}")
    return data["id"]
