"""The signed, short-lived gtwy link to a conversation's live browser view.

Whoever opens a live view can watch and drive that conversation's browser, so the chat never
carries the browser's own live URL. It carries this link instead: ``/browser/live/<token>``
resolves the conversation's browser when it is opened (``src/routes/v2/browserRouter.py``).
"""

from datetime import UTC, datetime, timedelta

import jwt

from config import Config

TOKEN_ALGORITHM = "HS256"
# Whoever holds the link can drive the browser, so it is short-lived.
LIVE_LINK_TTL_MINUTES = 15
# True once every UI opens the view through /browser/live-session (which checks the viewer's
# login): the plain link then stops working, so a forwarded or leaked link opens nothing.
REQUIRE_VIEWER_LOGIN = False


def signed_live_url(org_id, thread_id, sub_thread_id, viewer_id=None) -> str | None:
    """A short-lived gtwy link to this conversation's browser.

    The token names the conversation and, when known, the user it belongs to (``u``), so
    ``/browser/live-session`` can hand the view only to that user. It expires after
    LIVE_LINK_TTL_MINUTES; every tool result mints a fresh one. None when the gateway
    does not know its own public address; the caller then hands out the browser's direct link.
    """
    base = (getattr(Config, "GTWY_PUBLIC_URL", None) or "").rstrip("/")
    if not base or not Config.SecretKey:
        return None
    payload = {
        "o": str(org_id or ""),
        "t": str(thread_id or ""),
        "s": str(sub_thread_id or thread_id or ""),
        "exp": datetime.now(UTC) + timedelta(minutes=LIVE_LINK_TTL_MINUTES),
    }
    if viewer_id:
        payload["u"] = str(viewer_id)
    return f"{base}/browser/live/{jwt.encode(payload, Config.SecretKey, algorithm=TOKEN_ALGORITHM)}"
