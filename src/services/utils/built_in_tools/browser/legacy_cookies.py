"""Carry a conversation's login from the old Steel backend into its first Browser Use browser.

Steel saved each conversation's cookies, encrypted, in ``gtwy_browser_cookies``. When such a
conversation opens its first Browser Use browser (its profile was just created), those cookies
are loaded into the browser's default context once and the Mongo document is deleted. The
profile saves them from the next stop on, so the user stays signed in across the switch.

TODO: 30 days after the Browser Use switch reaches prod, delete this module with
``src/db_services/browserCookieService.py``; every old document has expired by then.
"""

import json

from globals import logger

from .connection import browser_cdp_send

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


async def import_once(browser, tkey: str) -> int:
    """Load this conversation's old Steel cookies into the browser, then forget them.
    Returns how many were loaded. Never raises: a failure only costs the user a fresh login."""
    from src.db_services.browserCookieService import delete_browser_cookies, get_browser_cookies
    from src.services.utils.helper import Helper  # imported here: helper imports back into this package

    try:
        document = await get_browser_cookies(tkey)
        if not document or not document.get("cookies"):
            return 0
        cookies = [
            {k: v for k, v in cookie.items() if k in SAFE_COOKIE_FIELDS}
            for cookie in json.loads(Helper.decrypt(document["cookies"])) or []
        ]
        if cookies:
            await browser_cdp_send(browser, "Storage.setCookies", {"cookies": cookies})
        await delete_browser_cookies(tkey)
        logger.info(f"Gtwy_Browser: moved {len(cookies)} saved Steel cookies of {tkey} into its browser")
        return len(cookies)
    except Exception as exc:
        logger.error(f"Gtwy_Browser: could not move saved cookies of {tkey}: {exc.__class__.__name__}: {exc}")
        return 0
