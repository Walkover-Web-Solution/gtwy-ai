"""Thin REST client for a self-hosted Steel browser (https://github.com/steel-dev/steel-browser).

Steel OSS runs ONE Chrome and ONE active session per container, and it always keeps a
session running: after any release it starts a default one on its own. Creating a session
relaunches Chrome, and so does releasing one, even with a stale or unknown id (verified
against Steel). Either call kills every tab of every conversation, so gtwy never releases
and only creates when Steel reports no session at all. The CDP endpoint is not tied to a
session: ``wss://host/`` reaches whatever Chrome is running right now, which is why the
caller compares the running session id with the one it remembered before trusting its tabs.
Steel OSS has no auth: keep it on a private network.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from config import Config
from globals import logger

# Creating a session relaunches Chrome, which is fast; a slower answer than this means Steel
# is unwell and retrying inside one tool call will not help.
STEEL_HTTP_TIMEOUT = 10.0
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
# The link sits in the chat history, so it must outlive the tab it resolves to.
LIVE_LINK_TTL_DAYS = 30


class SteelError(Exception):
    """Steel REST call failed or Steel is unreachable."""


def is_configured() -> bool:
    return bool(getattr(Config, "STEEL_API_URL", None))


def _base_url() -> str:
    return (Config.STEEL_API_URL or "").rstrip("/")


def steel_host() -> str | None:
    """Hostname of the Steel API, used by the SSRF guard so the model cannot browse Steel itself."""
    if not is_configured():
        return None
    return urlparse(_base_url()).hostname


def cdp_ws_url() -> str:
    """Websocket URL Playwright connects to. Steel OSS exposes CDP at the API root with no session id."""
    parsed = urlparse(_base_url())
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/"


def permanent_live_url(org_id, thread_id, sub_thread_id) -> str | None:
    """A link to this conversation's browser that keeps working after the tab is replaced.

    It points at gtwy rather than at Steel, so the tab is resolved when someone opens it.
    Falls back to None when the gateway does not know its own public address; the caller
    then hands out the direct Steel link, which is correct but only until that tab closes.
    """
    import jwt

    base = (getattr(Config, "GTWY_PUBLIC_URL", None) or "").rstrip("/")
    if not base or not Config.SecretKey:
        return None
    # The expiry is rounded to the start of today, so the same conversation gets the same link
    # all day instead of a new one per message. Each link stays valid for its full window, so a
    # link from an older message keeps working.
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    payload = {
        "o": str(org_id or ""),
        "t": str(thread_id or ""),
        "s": str(sub_thread_id or thread_id or ""),
        "exp": midnight + timedelta(days=LIVE_LINK_TTL_DAYS),
    }
    return f"{base}/browser/live/{jwt.encode(payload, Config.SecretKey, algorithm='HS256')}"


def live_view_url(debug_url: str | None, target_id: str | None) -> str | None:
    """Interactive live view pinned to one tab.

    Steel's session player takes a ``pageId`` query parameter and then streams and
    accepts input for just that tab, so each conversation's user only ever sees and
    types into their own page.
    """
    if not debug_url:
        return None
    base = debug_url
    if not target_id:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}pageId={target_id}"


def redact_session_id(session_id: str | None) -> str:
    if not session_id:
        return "<none>"
    return f"...{session_id[-6:]}"


async def _request(method: str, path: str, json_body: dict | None = None) -> dict | None:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=STEEL_HTTP_TIMEOUT) as client:
            response = await client.request(method, url, json=json_body)
    except httpx.HTTPError as exc:
        raise SteelError(f"steel unreachable: {exc.__class__.__name__}: {str(exc)[:120]}") from exc
    if response.status_code >= 400:
        raise SteelError(f"steel {method} {path} returned {response.status_code}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def pick_current_session(sessions: list[dict] | None) -> dict | None:
    """The session whose Chrome is running now: the newest one Steel has not released.

    Steel's list can carry a stale ``live`` entry next to the real one after a messy relaunch,
    so the newest wins rather than the first. Pure, so it can be tested without Steel.
    """
    live = [s for s in (sessions or []) if s.get("id") and s.get("status") != "released"]
    if not live:
        return None
    return max(live, key=lambda s: s.get("createdAt") or "")


async def current_session() -> dict | None:
    """Ask Steel which session is running. None means Steel has no Chrome up at all."""
    data = await _request("GET", "/v1/sessions")
    return pick_current_session((data or {}).get("sessions"))


async def create_session() -> dict:
    """Launch a Steel session. Only for when Steel reports none: this relaunches Chrome."""
    body = {"dimensions": DEFAULT_VIEWPORT, "blockAds": True}
    data = await _request("POST", "/v1/sessions", body)
    if not data or not data.get("id"):
        raise SteelError("steel returned no session id")
    logger.info(f"Gtwy_Browser: created steel session {redact_session_id(data['id'])}")
    return data
