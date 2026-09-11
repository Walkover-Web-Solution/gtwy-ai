"""Thin REST client for a self-hosted Steel browser (https://github.com/steel-dev/steel-browser).

Steel OSS runs ONE Chrome and ONE active session per container. Creating a new
session relaunches Chrome and kills the previous one, so all session ownership
is arbitrated by session_store, never here. Steel OSS has no auth: keep it on a
private network.
"""

from urllib.parse import urlparse

import httpx

from config import Config
from globals import logger

STEEL_HTTP_TIMEOUT = 15.0
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


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


async def create_session() -> dict:
    """Launch a Steel session. Returns the raw Steel session dict (id, websocketUrl, debugUrl, ...)."""
    body = {"dimensions": DEFAULT_VIEWPORT, "blockAds": True}
    data = await _request("POST", "/v1/sessions", body)
    if not data or not data.get("id"):
        raise SteelError("steel returned no session id")
    logger.info(f"Gtwy_Browser: created steel session {redact_session_id(data['id'])}")
    return data


async def release_session(session_id: str) -> bool:
    try:
        await _request("POST", f"/v1/sessions/{session_id}/release")
        logger.info(f"Gtwy_Browser: released steel session {redact_session_id(session_id)}")
        return True
    except SteelError as exc:
        # Steel answers with a synthetic "released" stub for unknown ids, so a failure here is rare
        # and never fatal: the next create_session() relaunches Chrome anyway.
        logger.warning(f"Gtwy_Browser: release of {redact_session_id(session_id)} failed: {exc}")
        return False
