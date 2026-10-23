"""Thin REST client for Anchor Browser sessions (https://docs.anchorbrowser.io).

Works like ``browser_use_client.py`` and returns the same browser view (``provider_base.py``):

* ``POST /v1/sessions`` starts a headful browser. It survives a CDP disconnect until its idle
  timeout (counted after every connection, including the live view, has closed) or its maximum
  duration. Recording is turned off: the live view is where users type passwords.
* Logins live in an Anchor profile, which Anchor names rather than numbers; each conversation
  gets a stable name (``profile_name``) and ``persist`` saves it when the session ends. If Anchor
  refuses an unknown name, the browser starts without it and the profile is created from that
  running session instead.
* Anchor's documented connect URL authenticates with the account key, so a key in any URL it
  returns is stripped before the record is stored and added back only when connecting. A live
  view URL that would carry the key is never handed out.
* CAPTCHA solving needs Anchor's proxy, so it is on only while the proxy is.
"""

from datetime import UTC, datetime, timedelta

from config import Config
from globals import logger

from .provider_base import (
    SECRET_PARAMS,
    BrowserServiceError,
    mentions_profile,
    profile_name,
    redact,
    request,
    with_param,
    without_param,
)

NAME = "anchor"
LABEL = "Anchor Browser"
API_URL = "https://api.anchorbrowser.io/v1"
CONNECT_URL = "wss://connect.anchorbrowser.io"
# Minutes. Same 4 hour ceiling as Browser Use; Anchor's own idle timeout matches the longest a
# user may spend logging in through the live view.
MAX_DURATION_MINUTES = 240
IDLE_TIMEOUT_MINUTES = 15
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
RAW_LIVE_URL_SHAREABLE = False
PROFILE_SAVE_SECONDS = 5
ENDED_STATUSES = {"completed", "ended", "terminated", "stopped", "closed", "failed", "expired", "timed_out"}


def is_configured() -> bool:
    return bool(getattr(Config, "ANCHOR_API_KEY", None))


async def _request(method: str, path: str, json_body: dict | None = None, params=None):
    headers = {"anchor-api-key": Config.ANCHOR_API_KEY or ""}
    return await request(LABEL, method, f"{API_URL}{path}", headers, json_body, params)


def _data(body) -> dict:
    return (body or {}).get("data") or {} if isinstance(body, dict) else {}


def _carries_key(url: str | None) -> bool:
    lowered = (url or "").lower()
    return any(f"{param.lower()}=" in lowered for param in SECRET_PARAMS)


def _view(data: dict) -> dict:
    status = str(data.get("status") or "").lower()
    live_url = data.get("live_view_url")
    if _carries_key(live_url):
        # Never stored or handed out: it would give the whole account to whoever opens it.
        logger.error(f"Gtwy_Browser: {LABEL} live view of {redact(data.get('id'))} carries a key; not shared")
        live_url = None
    return {
        "id": data.get("id") or data.get("session_id"),
        "cdp_url": without_param(data.get("cdp_url"), "apiKey"),
        "live_url": live_url,
        "timeout_at": None,
        "profile_id": None,
        "active": status not in ENDED_STATUSES,
        "started_at": data.get("created_at"),
    }


def _body(profile: str | None, metadata: dict | None, country: str | None) -> dict:
    session = {
        "timeout": {"max_duration": MAX_DURATION_MINUTES, "idle_timeout": IDLE_TIMEOUT_MINUTES},
        "proxy": {"active": True, "type": "anchor_proxy", "country_code": country} if country else {"active": False},
        "live_view": {"read_only": False},
        "recording": {"active": False},
    }
    if metadata:
        session["tags"] = [f"{k}:{v}" for k, v in metadata.items() if v is not None]
    browser = {"viewport": DEFAULT_VIEWPORT, "captcha_solver": {"active": bool(country)}}
    if profile:
        browser["profile"] = {"name": profile, "persist": True}
    return {"session": session, "browser": browser}


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    """Start one browser. ``profile_id`` is the conversation's profile name."""
    adopt_profile = False
    try:
        body = await _request("POST", "/sessions", _body(profile_id, metadata, country))
    except BrowserServiceError as exc:
        if not profile_id or exc.status not in (400, 404, 422) or not mentions_profile(exc):
            raise
        # Anchor does not know this name yet: start without it and create it from the session.
        body = await _request("POST", "/sessions", _body(None, metadata, country))
        adopt_profile = True
    view = _view(_data(body))
    if not view["id"] or not view["cdp_url"]:
        raise BrowserServiceError(f"{LABEL} returned no session id or cdp url")
    if adopt_profile:
        try:
            await _request("POST", "/profiles", {"name": profile_id, "source": "session", "session_id": view["id"]})
        except BrowserServiceError as exc:
            logger.warning(f"Gtwy_Browser: {LABEL} could not create profile {redact(profile_id)}: {exc}")
    logger.info(
        f"Gtwy_Browser: started {LABEL} session {redact(view['id'])} "
        f"(profile {redact(profile_id)}, proxy {country or 'none'}, max {MAX_DURATION_MINUTES} min)"
    )
    view["profile_id"] = profile_id
    view["timeout_at"] = (datetime.now(UTC) + timedelta(minutes=MAX_DURATION_MINUTES)).isoformat()
    return view


async def get_browser(browser_id: str) -> dict | None:
    try:
        body = await _request("GET", f"/sessions/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status == 404:
            return None
        raise
    data = _data(body)
    if not data:
        return None
    data.setdefault("id", browser_id)
    return _view(data)


async def stop_browser(browser_id: str) -> None:
    """End the session. Its profile, when persisted, is saved as it ends."""
    try:
        await _request("DELETE", f"/sessions/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status in (404, 410):
            return
        # Anchor answers 401 both for a bad key and for a session it no longer knows.
        if exc.status == 401 and await get_browser(browser_id) is None:
            return
        raise
    logger.info(f"Gtwy_Browser: stopped {LABEL} session {redact(browser_id)}")


async def list_active_browsers(env: str) -> list[dict] | None:
    """Running sessions tagged with this environment."""
    body = await _request("GET", "/sessions/all/status", params={"tags": f"gtwy_env:{env}"})
    views = [_view(item) for item in _data(body).get("items") or []]
    return [view for view in views if view["active"]]


async def create_profile(tkey: str, org_id=None) -> str | None:
    """Anchor profiles are named: the conversation's stable name, created with its first session."""
    return profile_name(tkey)


def connect_url(record: dict) -> str:
    url = record.get("cdp_url") or f"{CONNECT_URL}?sessionId={record['browser_id']}"
    return url if _carries_key(url) else with_param(url, "apiKey", Config.ANCHOR_API_KEY or "")


async def live_view_url(record: dict, view: dict | None = None) -> str | None:
    return (view or {}).get("live_url") or record.get("live_url")
