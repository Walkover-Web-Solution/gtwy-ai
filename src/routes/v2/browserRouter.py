"""Links to a conversation's browser live view.

Whoever opens a live view can watch and drive that conversation's browser, including a page
where its user is signed in, so the view is handed out through a gtwy token, never stored as
the backend's own URL:

* ``GET /browser/live/{token}`` is the plain link the chat carries. It resolves the
  conversation's browser at the moment it is opened and redirects to its live view, or says
  plainly that no browser is open instead of spinning. The token expires after
  LIVE_LINK_TTL_MINUTES. It checks nothing about the viewer, so with
  REQUIRE_VIEWER_LOGIN (``live_link.py``) it refuses and the UI must use the route below.
* ``GET /browser/live-session/{token}`` is the same lookup for a signed-in viewer. The caller
  sends its usual Authorization header (a gtwy login, or a chatbot end user's token); the view is
  returned only when the caller's org is the conversation's org and, when the token names the
  conversation's user, the caller is that user.
"""

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import Config
from globals import logger
from src.middlewares.interfaceMiddlewares import chat_bot_auth
from src.middlewares.middleware import jwt_middleware
from src.services.utils.built_in_tools.browser import browser_use_sessions
from src.services.utils.built_in_tools.browser.live_link import REQUIRE_VIEWER_LOGIN, TOKEN_ALGORITHM
from src.services.utils.built_in_tools.browser.session_store import thread_key

router = APIRouter()

NO_BROWSER = (
    "No browser is open for this conversation",
    "The browser closed after a few minutes of inactivity or was restarted. Ask the assistant to "
    "open a page again and a fresh one will start.",
    404,
)
UNAVAILABLE = ("The browser view is unavailable", "Try again in a moment.", 503)


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


def _decode(token: str) -> tuple[dict | None, tuple | None]:
    """(claims, None), or (None, (title, detail, status)) for an expired or forged token."""
    try:
        return jwt.decode(token, Config.SecretKey, algorithms=[TOKEN_ALGORITHM]), None
    except jwt.ExpiredSignatureError:
        return None, ("This link has expired", "Ask the assistant to open the browser again.", 410)
    except jwt.InvalidTokenError:
        return None, ("This link is not valid", "Check that you copied the whole address.", 400)


async def _resolve_target(scope: str) -> tuple[str | None, tuple | None]:
    """(live view URL, None) for the conversation's running browser, or (None, problem).
    Each conversation has its own browser, so the view can only ever show its own pages."""
    try:
        target = await browser_use_sessions.live_target(scope)
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: live view lookup failed for {scope}: {exc.__class__.__name__}: {exc}")
        return None, UNAVAILABLE
    return (target, None) if target else (None, NO_BROWSER)


def _scope(claims: dict) -> str:
    return thread_key(claims.get("o"), claims.get("t"), claims.get("s"))


@router.get("/live/{token}")
async def live_view(token: str):
    """Send whoever holds the link to this conversation's browser as it is right now."""
    claims, problem = _decode(token)
    if problem:
        return _page(*problem)
    if REQUIRE_VIEWER_LOGIN:
        return _page("Open the browser from the chat", "For your security this link only works inside the chat window.", 403)
    target, problem = await _resolve_target(_scope(claims))
    if problem:
        return _page(*problem)
    return RedirectResponse(target, status_code=302)


def _signed_by_gtwy(header: str) -> bool:
    try:
        jwt.decode(header, Config.SecretKey, algorithms=[TOKEN_ALGORITHM])
        return True
    except jwt.InvalidTokenError:
        return False


async def live_viewer_auth(request: Request) -> None:
    """Identify the caller from whichever login its Authorization header carries: a gtwy user
    (dashboard, playground, embed, pauthkey) or a chatbot end user. Sets request.state.profile."""
    header = request.headers.get("Authorization") or ""
    if request.headers.get("proxy_auth_token") or request.headers.get("pauthkey") or _signed_by_gtwy(header):
        await jwt_middleware(request)
    else:
        await chat_bot_auth(request)


@router.get("/live-session/{token}", dependencies=[Depends(live_viewer_auth)])
async def live_session(token: str, request: Request):
    """The live view URL for a signed-in viewer, as JSON, for the UI to put in its iframe.

    Only the conversation's org gets it, and only the conversation's own user when the token
    names one. The URL itself is a credential for that one browser, so it is never cached.
    """
    claims, problem = _decode(token)
    if problem:
        raise HTTPException(status_code=problem[2], detail=problem[0])
    profile = getattr(request.state, "profile", None) or {}
    caller_org = str((profile.get("org") or {}).get("id") or "")
    caller_user = str((profile.get("user") or {}).get("id") or "")
    if caller_org != str(claims.get("o") or ""):
        logger.warning(f"Gtwy_Browser: live view refused: org {caller_org} asked for a browser of org {claims.get('o')}")
        raise HTTPException(status_code=403, detail="this browser belongs to another organization")
    if claims.get("u") and caller_user != str(claims["u"]):
        logger.warning(f"Gtwy_Browser: live view refused: user {caller_user} asked for the browser of another user in org {caller_org}")
        raise HTTPException(status_code=403, detail="this browser belongs to another user")
    target, problem = await _resolve_target(_scope(claims))
    if problem:
        raise HTTPException(status_code=problem[2], detail=problem[0])
    return JSONResponse({"live_url": target}, headers={"Cache-Control": "no-store"})
