"""A permanent link to a conversation's browser tab.

A live view URL names one Chrome tab by id, and that tab is closed once the
conversation goes quiet. A link captured a few messages ago therefore points at a
tab that no longer exists, and Steel's player answers by retrying forever, which
reads as "Session connecting..." with no explanation.

This route gives each conversation one address that never changes. It resolves the
tab at the moment someone opens it, so an old link still works, and when no browser
is open it says so instead of spinning.
"""

import jwt
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from config import Config
from globals import logger
from src.services.utils.built_in_tools.browser import steel_client
from src.services.utils.built_in_tools.browser.session_store import get_registry, thread_key
from src.services.utils.built_in_tools.browser.steel_client import SteelError

router = APIRouter()

TOKEN_ALGORITHM = "HS256"
# The link lives in the chat history, so it has to outlive the conversation's tab by a long way.
TOKEN_TTL_DAYS = 30


def _page(title: str, detail: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        status_code=status,
        content=(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Browser view</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;"
            "min-height:100vh;background:#0e141a;color:#e7edf3}main{max-width:32rem;padding:2rem;text-align:center}"
            "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0;color:#9aa9b6;line-height:1.6}</style>"
            f"<main><h1>{title}</h1><p>{detail}</p></main>"
        ),
    )


@router.get("/live/{token}")
async def live_view(token: str):
    """Send the viewer to this conversation's browser tab as it is right now."""
    try:
        claims = jwt.decode(token, Config.SecretKey, algorithms=[TOKEN_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return _page("This link has expired", "Ask the assistant to open the browser again.", 410)
    except jwt.InvalidTokenError:
        return _page("This link is not valid", "Check that you copied the whole address.", 400)

    scope = thread_key(claims.get("o"), claims.get("t"), claims.get("s"))
    registry = await get_registry()
    tab = ((registry or {}).get("tabs") or {}).get(scope)

    if tab and tab.get("target_id"):
        # The tab record may outlive its Chrome: if Steel is running a different session than the
        # one the tab was opened in, the tab is gone and Steel's player would spin on it forever.
        try:
            current = await steel_client.current_session()
        except SteelError as exc:
            logger.warning(f"Gtwy_Browser: live view could not check the current session ({exc})")
            current = None
        if current is not None and current.get("id") != registry.get("steel_session_id"):
            tab = None

    if not tab or not tab.get("target_id"):
        return _page(
            "No browser is open for this conversation",
            "The tab closed after a few minutes of inactivity or the browser was restarted. Ask "
            "the assistant to open a page again and a fresh one will start.",
            404,
        )

    target = steel_client.live_view_url(registry.get("debug_url"), tab["target_id"])
    if not target:
        logger.warning("Gtwy_Browser: live view requested but the browser has no debug url")
        return _page("The browser view is unavailable", "Try again in a moment.", 503)
    return RedirectResponse(target, status_code=302)
