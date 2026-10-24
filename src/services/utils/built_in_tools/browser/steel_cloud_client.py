"""Thin REST client for Steel Cloud sessions (https://docs.steel.dev), the hosted Steel service.

Works like ``browser_use_client.py`` and returns the same browser view (``provider_base.py``):

* ``POST /v1/sessions`` starts a browser. It runs until it is released or its ``timeout``
  passes; disconnecting does not stop it. The longest allowed timeout depends on the plan
  (15 min on the lowest one), so a refused timeout is retried with the shortest one; a long
  conversation then simply gets a new browser that opens with the same saved logins.
* Logins live in a Steel profile. Steel creates it from the first session started with
  ``persistProfile`` and no ``profileId``, so ``create_profile`` returns None and the profile id
  comes back from ``create_browser``. The profile is uploaded after a release; while it uploads
  a new session using it is refused with 409 (ProfileBusy).
* Steel sessions carry no metadata, so the stray sweep cannot tell this environment's browsers
  from another's: ``list_active_browsers`` returns None and the sweep skips Steel. Use one
  Steel API key per environment.
* The CDP URL needs the account key, so it is added only when connecting. The debug URL is
  unauthenticated but scoped to one session.
"""

from datetime import UTC, datetime, timedelta

from config import Config
from globals import logger

from .provider_base import (
    BrowserServiceError,
    ProfileBusy,
    ProfileNotFound,
    mentions_profile,
    redact,
    request,
    with_param,
    without_param,
)

NAME = "steel"
LABEL = "Steel"
API_URL = "https://api.steel.dev/v1"
CONNECT_URL = "wss://connect.steel.dev"
# Milliseconds. The Scale plan allows an hour; the lowest plan allows 15 minutes, used when the
# longer one is refused.
SESSION_TIMEOUT_MS = 60 * 60 * 1000
FALLBACK_TIMEOUT_MS = 15 * 60 * 1000
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
RAW_LIVE_URL_SHAREABLE = False
PROFILE_SAVE_SECONDS = 5
ACTIVE_STATUSES = {"live"}


def is_configured() -> bool:
    return bool(getattr(Config, "STEEL_API_KEY", None))


async def _request(method: str, path: str, json_body: dict | None = None, params=None):
    headers = {"steel-api-key": Config.STEEL_API_KEY or ""}
    return await request(LABEL, method, f"{API_URL}{path}", headers, json_body, params)


def _timeout_at(data: dict) -> str | None:
    created = data.get("createdAt")
    timeout = data.get("timeout")
    if not created or not timeout:
        return None
    try:
        start = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    return (start + timedelta(milliseconds=int(timeout))).isoformat()


def _live(debug_url: str | None) -> str | None:
    return with_param(debug_url, "interactive", "true") if debug_url else None


def _view(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "cdp_url": without_param(data.get("websocketUrl"), "apiKey") or f"{CONNECT_URL}?sessionId={data.get('id')}",
        "live_url": _live(data.get("debugUrl")),
        "timeout_at": _timeout_at(data),
        "profile_id": data.get("profileId"),
        "active": data.get("status") in ACTIVE_STATUSES,
        "started_at": data.get("createdAt"),
    }


async def _start(body: dict) -> dict:
    try:
        return await _request("POST", "/sessions", body)
    except BrowserServiceError as exc:
        if exc.status == 409:
            raise ProfileBusy(str(exc), exc.status, exc.detail) from exc
        if exc.status == 404 and mentions_profile(exc):
            raise ProfileNotFound(str(exc), exc.status, exc.detail) from exc
        raise


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    body = {
        "timeout": SESSION_TIMEOUT_MS,
        "dimensions": DEFAULT_VIEWPORT,
        "solveCaptcha": True,
        # Saves the browser's state into the profile when the session is released; without a
        # profileId Steel creates a new profile and returns its id.
        "persistProfile": True,
        "useProxy": {"geolocation": {"country": country.upper()}} if country else False,
    }
    if profile_id:
        body["profileId"] = profile_id
    try:
        data = await _start(body)
    except (ProfileBusy, ProfileNotFound):
        raise
    except BrowserServiceError as exc:
        if exc.status not in (400, 403, 422) or "timeout" not in f"{exc.detail} {exc}".lower():
            raise
        body["timeout"] = FALLBACK_TIMEOUT_MS
        data = await _start(body)
    if not data or not data.get("id"):
        raise BrowserServiceError(f"{LABEL} returned no session id")
    logger.info(
        f"Gtwy_Browser: started {LABEL} session {redact(data['id'])} "
        f"(profile {redact(profile_id or data.get('profileId'))}, proxy {country or 'none'}, "
        f"timeout {body['timeout'] // 60000} min)"
    )
    view = _view(data)
    view["profile_id"] = view["profile_id"] or profile_id
    if not view["timeout_at"]:
        view["timeout_at"] = (datetime.now(UTC) + timedelta(milliseconds=body["timeout"])).isoformat()
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
    """Release the session. Its profile is saved as it is released."""
    try:
        await _request("POST", f"/sessions/{browser_id}/release")
    except BrowserServiceError as exc:
        if exc.status in (404, 409, 410):
            return
        raise
    logger.info(f"Gtwy_Browser: stopped {LABEL} session {redact(browser_id)}")


async def list_active_browsers(env: str) -> list[dict] | None:
    """Steel sessions have no metadata to filter on, so the stray sweep skips Steel."""
    return None


async def create_profile(tkey: str, org_id=None) -> str | None:
    """Steel creates the profile from the conversation's first session."""
    return None


def connect_url(record: dict) -> str:
    return with_param(record["cdp_url"], "apiKey", Config.STEEL_API_KEY or "")


async def live_view_url(record: dict, view: dict | None = None) -> str | None:
    return (view or {}).get("live_url") or record.get("live_url")
