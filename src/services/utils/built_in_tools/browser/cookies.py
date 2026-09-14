"""Remember a conversation's browser logins so a new tab resumes signed in.

A tab's cookies live in its own jar and die with the tab, so a user who logged in
yesterday would have to log in again today. Before a tab closes its cookies are
exported, encrypted and stored; when the conversation opens a new tab they go back in
and the sites see the user as still signed in.

MongoDB is the only place a login lives. A cache in front of it was tried and removed:
the blob is read once per tab creation, roughly once per conversation, so a cache saved
almost nothing while giving a login a second home and a way to go stale. The scope is
the conversation, matching one tab per thread and sub-thread.

Cookies are full account access. Only the encrypted blob is ever stored, it is scoped
to one conversation, and it expires on its own.
"""

import json

from config import Config
from globals import logger

from .connection import export_jar_cookies, import_jar_cookies

# Chrome returns extra read-only fields (size, session, sameParty...) that Storage.setCookies
# rejects, so only these are kept.
SAFE_COOKIE_FIELDS = {
    "name",
    "value",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "expires",
    "priority",
    "sourceScheme",
    "sourcePort",
}


# How long a saved login stays valid, stamped onto the Mongo document.
COOKIE_TTL_SECONDS = 30 * 86400


def is_enabled() -> bool:
    """Cookies are only stored when we can encrypt them."""
    if not (Config.Encreaption_key and Config.Secret_IV):
        logger.warning("Gtwy_Browser: encryption keys are missing; logins will not be saved")
        return False
    return True


def _trim(cookies: list[dict]) -> list[dict]:
    return [{k: v for k, v in cookie.items() if k in SAFE_COOKIE_FIELDS} for cookie in (cookies or [])]


async def _load_blob(scope_key: str) -> str | None:
    """The encrypted blob stored for this conversation, or None."""
    from src.db_services.browserCookieService import get_browser_cookies

    document = await get_browser_cookies(scope_key)
    if not document or not document.get("cookies"):
        return None
    return document["cookies"]


async def _store_blob(scope_key: str, blob: str, cookie_count: int, meta: dict | None) -> None:
    """Persist this conversation's cookies. A failure only costs the user a fresh login."""
    from src.db_services.browserCookieService import upsert_browser_cookies

    if not await upsert_browser_cookies(scope_key, blob, cookie_count, COOKIE_TTL_SECONDS, meta):
        logger.error(f"Gtwy_Browser: could not persist cookies for {scope_key}; the user will sign in again")


async def save_jar(browser, browser_context_id: str | None, scope_key: str | None, meta: dict | None = None) -> int:
    """Export a tab's cookies and store them for its conversation. Returns how many were saved."""
    if not is_enabled() or not browser_context_id or not scope_key:
        return 0

    from src.services.utils.helper import Helper  # imported here: helper imports back into this package

    try:
        cookies = _trim(await export_jar_cookies(browser, browser_context_id))
        if not cookies:
            return 0
        await _store_blob(scope_key, Helper.encrypt(json.dumps(cookies)), len(cookies), meta)
        logger.info(f"Gtwy_Browser: saved {len(cookies)} cookies for {scope_key}")
        return len(cookies)
    except Exception as exc:
        # Never let a cookie problem break the browser flow: the user just logs in again.
        logger.error(f"Gtwy_Browser: could not save cookies for {scope_key}: {exc.__class__.__name__}: {exc}")
        return 0


async def restore_jar(browser, browser_context_id: str | None, scope_key: str | None) -> int:
    """Put a conversation's saved cookies into a fresh tab. Returns how many were restored."""
    if not is_enabled() or not browser_context_id or not scope_key:
        return 0

    from src.services.utils.helper import Helper  # see save_jar

    try:
        blob = await _load_blob(scope_key)
        if not blob:
            return 0
        cookies = json.loads(Helper.decrypt(blob))
        applied = await import_jar_cookies(browser, browser_context_id, cookies)
        logger.info(f"Gtwy_Browser: restored {applied} cookies for {scope_key}")
        return applied
    except Exception as exc:
        logger.error(f"Gtwy_Browser: could not restore cookies for {scope_key}: {exc.__class__.__name__}: {exc}")
        return 0
