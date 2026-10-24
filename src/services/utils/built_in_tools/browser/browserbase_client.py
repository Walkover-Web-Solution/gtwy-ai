"""Thin REST client for Browserbase sessions (https://docs.browserbase.com).

Works like ``browser_use_client.py`` and returns the same browser view (``provider_base.py``):

* ``POST /v1/sessions`` starts a browser in our project. ``keepAlive`` keeps it running when a
  CDP client disconnects (a pod restart, or Browserbase closing a connection idle for 10 min);
  without it the session ends with the connection. Keep-alive, proxies and CAPTCHA solving
  need a paid plan; the free plan's sessions are refused with a clear error, never weakened.
* A conversation's logins live in a Browserbase context (``browserSettings.context`` with
  ``persist``), saved when the session ends. Browserbase asks for a few seconds after that
  before the context is used again.
* The live view is not in the session itself: ``GET /v1/sessions/{id}/debug`` returns one
  interactive URL per tab, and the conversation's current tab is picked.
* Session recording and logging are turned off: the live view is where users type passwords.
"""

from datetime import UTC, datetime, timedelta

from config import Config
from globals import logger

from .provider_base import (
    BrowserServiceError,
    ProfileNotFound,
    mentions_profile,
    redact,
    request,
    with_param,
    without_param,
)

NAME = "browserbase"
LABEL = "Browserbase"
API_URL = "https://api.browserbase.com/v1"
# Seconds; Browserbase allows 60..21600. Same 4 hour ceiling as Browser Use; the reaper stops
# idle browsers long before it.
BROWSER_TIMEOUT_SECONDS = 4 * 3600
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
RAW_LIVE_URL_SHAREABLE = False
PROFILE_SAVE_SECONDS = 5
ACTIVE_STATUSES = {"RUNNING", "PENDING"}


def is_configured() -> bool:
    return bool(getattr(Config, "BROWSERBASE_API_KEY", None) and getattr(Config, "BROWSERBASE_PROJECT_ID", None))


async def _request(method: str, path: str, json_body: dict | None = None, params=None):
    headers = {"X-BB-API-Key": Config.BROWSERBASE_API_KEY or ""}
    return await request(LABEL, method, f"{API_URL}{path}", headers, json_body, params)


def _view(data: dict) -> dict:
    # A connect URL that carries the account key is stored without it; connect_url adds it back.
    return {
        "id": data.get("id"),
        "cdp_url": without_param(data.get("connectUrl"), "apiKey"),
        "live_url": None,
        "timeout_at": data.get("expiresAt"),
        "profile_id": data.get("contextId"),
        "active": data.get("status") in ACTIVE_STATUSES,
        "started_at": data.get("startedAt") or data.get("createdAt"),
    }


def _proxies(country: str | None):
    if not country:
        return False
    return [{"type": "browserbase", "geolocation": {"country": country.upper()}}]


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    browser_settings = {
        "viewport": DEFAULT_VIEWPORT,
        "solveCaptchas": True,
        "recordSession": False,
        "logSession": False,
    }
    if profile_id:
        browser_settings["context"] = {"id": profile_id, "persist": True}
    body = {
        "projectId": Config.BROWSERBASE_PROJECT_ID,
        "keepAlive": True,
        "timeout": BROWSER_TIMEOUT_SECONDS,
        "browserSettings": browser_settings,
        "proxies": _proxies(country),
    }
    if metadata:
        body["userMetadata"] = {str(k): str(v)[:100] for k, v in metadata.items() if v is not None}
    try:
        data = await _request("POST", "/sessions", body)
    except BrowserServiceError as exc:
        if exc.status in (400, 404) and mentions_profile(exc):
            raise ProfileNotFound(str(exc), exc.status, exc.detail) from exc
        raise
    if not data or not data.get("id") or not data.get("connectUrl"):
        raise BrowserServiceError(f"{LABEL} returned no session id or connect url")
    logger.info(
        f"Gtwy_Browser: started {LABEL} session {redact(data['id'])} "
        f"(context {redact(profile_id)}, proxy {country or 'none'}, timeout {BROWSER_TIMEOUT_SECONDS // 60} min)"
    )
    view = _view(data)
    view["profile_id"] = view["profile_id"] or profile_id
    if not view["timeout_at"]:
        view["timeout_at"] = (datetime.now(UTC) + timedelta(seconds=BROWSER_TIMEOUT_SECONDS)).isoformat()
    return view


async def get_browser(browser_id: str) -> dict | None:
    try:
        data = await _request("GET", f"/sessions/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status == 404:
            return None
        raise
    return _view(data) if data else None


async def stop_browser(browser_id: str) -> None:
    """Release the session. Its context, if persisted, is saved as it closes."""
    try:
        await _request(
            "POST",
            f"/sessions/{browser_id}",
            {"projectId": Config.BROWSERBASE_PROJECT_ID, "status": "REQUEST_RELEASE"},
        )
    except BrowserServiceError as exc:
        # Already ended: there is nothing left to stop.
        if exc.status in (404, 409, 410):
            return
        raise
    logger.info(f"Gtwy_Browser: stopped {LABEL} session {redact(browser_id)}")


async def list_active_browsers(env: str) -> list[dict] | None:
    """Running sessions tagged with this environment."""
    params = {"status": "RUNNING", "q": f"user_metadata['gtwy_env']:'{env}'"}
    data = await _request("GET", "/sessions", params=params)
    items = data if isinstance(data, list) else (data or {}).get("items") or []
    return [_view(item) for item in items]


async def create_profile(tkey: str, org_id=None) -> str | None:
    """Create an empty context for this conversation and return its id."""
    data = await _request("POST", "/contexts", {"projectId": Config.BROWSERBASE_PROJECT_ID})
    if not data or not data.get("id"):
        raise BrowserServiceError(f"{LABEL} returned no context id")
    logger.info(f"Gtwy_Browser: created {LABEL} context {redact(data['id'])}")
    return data["id"]


def connect_url(record: dict) -> str:
    url = record["cdp_url"]
    # Browserbase's own connect URL may authenticate with the account key; it was stripped
    # before the record was stored.
    if "signingKey" not in url and "apiKey" not in url:
        url = with_param(url, "apiKey", Config.BROWSERBASE_API_KEY or "")
    return url


async def live_view_url(record: dict, view: dict | None = None) -> str | None:
    """The interactive live view of the conversation's current tab (else the whole browser)."""
    data = await _request("GET", f"/sessions/{record['browser_id']}/debug")
    if not data:
        return None
    target_id = record.get("target_id")
    for page in data.get("pages") or []:
        if target_id and page.get("id") == target_id and page.get("debuggerFullscreenUrl"):
            return page["debuggerFullscreenUrl"]
    return data.get("debuggerFullscreenUrl")
