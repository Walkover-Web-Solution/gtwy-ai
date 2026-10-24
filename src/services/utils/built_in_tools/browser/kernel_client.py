"""Thin REST client for Kernel browsers (https://kernel.sh/docs).

Works like ``browser_use_client.py`` and returns the same browser view (``provider_base.py``):

* ``POST /browsers`` starts a headful browser (live view needs headful) with stealth on, which
  also brings Kernel's CAPTCHA solver. A browser survives a CDP disconnect.
* ``timeout_seconds`` is an inactivity timeout counted only while no CDP or live-view client is
  connected, so it cleans up after a pod crash; the reaper still stops idle browsers. gtwy pods
  keep their CDP connection open, so Kernel's zero-cost standby does not kick in and a browser
  bills headful time until it is stopped.
* A residential proxy is a separate Kernel object, created once per country and found again by
  name. ``proxy_country`` "none" uses ``{"mode": "direct"}``.
* Logins live in a Kernel profile (``save_changes``), saved when the browser is deleted. The
  browser that ends last overwrites a profile, so one conversation uses one profile.
"""

from config import Config
from globals import logger

from .provider_base import BrowserServiceError, ProfileNotFound, mentions_profile, profile_name, redact, request

NAME = "kernel"
LABEL = "Kernel"
API_URL = "https://api.onkernel.com"
# Seconds of inactivity (no CDP or live-view connection) before Kernel deletes the browser by
# itself; at least the 15 minutes a user may spend logging in through the live view.
INACTIVITY_TIMEOUT_SECONDS = 900
RAW_LIVE_URL_SHAREABLE = False
PROFILE_SAVE_SECONDS = 5

_proxy_ids: dict[str, str] = {}


def is_configured() -> bool:
    return bool(getattr(Config, "KERNEL_API_KEY", None))


async def _request(method: str, path: str, json_body: dict | None = None, params=None):
    headers = {"Authorization": f"Bearer {Config.KERNEL_API_KEY or ''}"}
    return await request(LABEL, method, f"{API_URL}{path}", headers, json_body, params)


def _items(data) -> list:
    if isinstance(data, list):
        return data
    return list((data or {}).get("items") or (data or {}).get("data") or [])


def _view(data: dict) -> dict:
    return {
        "id": data.get("session_id") or data.get("id"),
        "cdp_url": data.get("cdp_ws_url"),
        "live_url": data.get("browser_live_view_url"),
        # Kernel enforces no absolute lifetime, only the inactivity timeout.
        "timeout_at": None,
        "profile_id": (data.get("profile") or {}).get("id"),
        "active": not data.get("deleted_at"),
        "started_at": data.get("created_at"),
    }


async def _proxy_id(country: str) -> str:
    """The id of this environment's residential proxy for one country, created on first use."""
    if country in _proxy_ids:
        return _proxy_ids[country]
    name = f"gtwy-residential-{country}"
    existing = next((p for p in _items(await _request("GET", "/proxies")) if p.get("name") == name), None)
    if existing is None:
        try:
            existing = await _request(
                "POST", "/proxies", {"type": "residential", "name": name, "config": {"country": country.upper()}}
            )
        except BrowserServiceError as exc:
            if exc.status != 409:
                raise
            existing = next((p for p in _items(await _request("GET", "/proxies")) if p.get("name") == name), None)
    if not existing or not existing.get("id"):
        raise BrowserServiceError(f"{LABEL} returned no proxy id for country {country}")
    _proxy_ids[country] = existing["id"]
    return existing["id"]


async def create_browser(profile_id: str | None, metadata: dict | None = None, country: str | None = None) -> dict:
    body = {
        "headless": False,
        "stealth": True,
        "timeout_seconds": INACTIVITY_TIMEOUT_SECONDS,
        "proxy": {"id": await _proxy_id(country)} if country else {"mode": "direct"},
    }
    if profile_id:
        body["profile"] = {"id": profile_id, "save_changes": True}
    if metadata:
        body["tags"] = {str(k): str(v) for k, v in metadata.items() if v is not None}
    try:
        data = await _request("POST", "/browsers", body)
    except BrowserServiceError as exc:
        if exc.status in (400, 404) and mentions_profile(exc):
            raise ProfileNotFound(str(exc), exc.status, exc.detail) from exc
        raise
    view = _view(data or {})
    if not view["id"] or not view["cdp_url"]:
        raise BrowserServiceError(f"{LABEL} returned no browser id or cdp url")
    logger.info(
        f"Gtwy_Browser: started {LABEL} browser {redact(view['id'])} "
        f"(profile {redact(profile_id)}, proxy {country or 'none'})"
    )
    view["profile_id"] = view["profile_id"] or profile_id
    return view


async def get_browser(browser_id: str) -> dict | None:
    """The browser's view, or None once Kernel has deleted it."""
    try:
        data = await _request("GET", f"/browsers/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status == 404:
            return None
        raise
    return _view(data) if data else None


async def stop_browser(browser_id: str) -> None:
    """Delete the browser. Its profile is saved as it goes."""
    try:
        await _request("DELETE", f"/browsers/{browser_id}")
    except BrowserServiceError as exc:
        if exc.status in (404, 410):
            return
        raise
    logger.info(f"Gtwy_Browser: stopped {LABEL} browser {redact(browser_id)}")


async def list_active_browsers(env: str) -> list[dict] | None:
    """Running browsers tagged with this environment."""
    params = {"status": "active", "tags[gtwy_env]": env, "limit": 100}
    return [_view(item) for item in _items(await _request("GET", "/browsers", params=params))]


async def create_profile(tkey: str, org_id=None) -> str | None:
    """Create this conversation's profile (or find it again by its name) and return its id."""
    name = profile_name(tkey)
    try:
        data = await _request("POST", "/profiles", {"name": name})
    except BrowserServiceError as exc:
        if exc.status != 409:
            raise
        data = await _request("GET", f"/profiles/{name}")
    if not data or not data.get("id"):
        raise BrowserServiceError(f"{LABEL} returned no profile id")
    logger.info(f"Gtwy_Browser: created {LABEL} profile {redact(data['id'])}")
    return data["id"]


def connect_url(record: dict) -> str:
    return record["cdp_url"]


async def live_view_url(record: dict, view: dict | None = None) -> str | None:
    return (view or {}).get("live_url") or record.get("live_url")
