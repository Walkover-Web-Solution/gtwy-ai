"""Thin REST client for Browser Use Cloud browsers (https://docs.browser-use.com, API v4).

Only the browser infrastructure is used: ``POST /browsers`` starts a hosted Chromium (stealth
on by default) and returns its CDP URL and live view URL, and gtwy drives it with Playwright.
The Agent API (``/runs``) is not used: the model of the gtwy agent keeps deciding every click.

Each browser is its own Chromium, so one conversation's browser can never show another
conversation's pages. A browser runs until it is stopped or its ``timeout`` (at most 240
minutes) passes; disconnecting CDP does not stop it, so the reaper stops idle ones.
Both URLs a browser returns are bearer credentials: anyone holding them can control it.
The functions and the view they return are the same in every service client (see
``provider_base.py``).
"""

from config import Config
from globals import logger

from .provider_base import BrowserServiceError, ProfileNotFound, mentions_profile, redact, request

NAME = "browser_use"
LABEL = "Browser Use"
API_URL = "https://api.browser-use.com/api/v4"
# Hard lifetime of one browser; Browser Use allows at most 240 minutes. The reaper stops idle
# browsers long before this, so it only matters for a conversation that stays busy.
BROWSER_TIMEOUT_MINUTES = 240
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
# The live URL is only usable for this one browser, so it may be handed out when the gateway has
# no public address for its own signed link.
RAW_LIVE_URL_SHAREABLE = True
# Seconds the profile needs after a stop before the next browser may load it.
PROFILE_SAVE_SECONDS = 0


def is_configured() -> bool:
    return bool(getattr(Config, "BROWSER_USE_API_KEY", None))


async def _request(method: str, path: str, json_body: dict | None = None, params=None):
    headers = {"X-Browser-Use-API-Key": Config.BROWSER_USE_API_KEY or ""}
    return await request(LABEL, method, f"{API_URL}{path}", headers, json_body, params)


def _view(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "cdp_url": data.get("cdpUrl"),
        "live_url": data.get("liveUrl"),
        "timeout_at": data.get("timeoutAt"),
        "profile_id": data.get("profileId"),
        "active": data.get("status") == "active",
        "started_at": data.get("startedAt"),
    }


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    """Start one hosted browser and return its view.

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
    try:
        data = await _request("POST", "/browsers", body)
    except BrowserServiceError as exc:
        if exc.status == 404 and mentions_profile(exc):
            raise ProfileNotFound(str(exc), exc.status, exc.detail) from exc
        raise
    if not data or not data.get("id") or not data.get("cdpUrl"):
        raise BrowserServiceError(f"{LABEL} returned no browser id or cdp url")
    logger.info(
        f"Gtwy_Browser: started {LABEL} browser {redact(data['id'])} "
        f"(profile {redact(profile_id)}, proxy {country or 'none'}, timeout {BROWSER_TIMEOUT_MINUTES} min)"
    )
    view = _view(data)
    view["profile_id"] = view["profile_id"] or profile_id
    return view


async def get_browser(browser_id: str) -> dict | None:
    """The browser's current view, or None when Browser Use does not know it."""
    try:
        data = await _request("GET", f"/browsers/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status == 404:
            return None
        raise
    return _view(data) if data else None


async def stop_browser(browser_id: str) -> None:
    """Stop a browser for good. Its profile, if any, is saved when it stops."""
    try:
        await _request("PATCH", f"/browsers/{browser_id}", {"action": "stop"})
    except BrowserServiceError as exc:
        if exc.status == 404:
            return
        raise
    logger.info(f"Gtwy_Browser: stopped {LABEL} browser {redact(browser_id)}")


async def list_active_browsers(env: str, page_size: int = 100) -> list[dict] | None:
    """Running browsers tagged with this environment."""
    params = [("filterBy", "active"), ("pageSize", page_size), ("metadata", f"gtwy_env={env}")]
    data = await _request("GET", "/browsers", params=params)
    return [_view(item) for item in (data or {}).get("items") or []]


async def create_profile(tkey: str, org_id=None) -> str | None:
    """Create an empty profile for this conversation and return its id."""
    body = {"name": f"gtwy {tkey}"[:200]}
    if org_id:
        body["userId"] = str(org_id)
    data = await _request("POST", "/profiles", body)
    if not data or not data.get("id"):
        raise BrowserServiceError(f"{LABEL} returned no profile id")
    logger.info(f"Gtwy_Browser: created {LABEL} profile {redact(data['id'])}")
    return data["id"]


def connect_url(record: dict) -> str:
    return record["cdp_url"]


async def live_view_url(record: dict, view: dict | None = None) -> str | None:
    return (view or {}).get("live_url") or record.get("live_url")
