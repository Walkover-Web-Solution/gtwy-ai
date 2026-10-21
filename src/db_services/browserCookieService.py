"""Read-only access to the logins saved by the old Steel browser backend.

Steel kept one document per conversation in ``gtwy_browser_cookies`` holding an encrypted
cookie blob. Browser Use keeps logins in its own profiles now, so these documents are only
read once, to carry a conversation's login into its first Browser Use browser
(``browser/legacy_cookies.py``), and then deleted. The collection's TTL index removes the
rest on its own within 30 days of the switch.

TODO: 30 days after the Browser Use switch reaches prod, delete this module,
``browser/legacy_cookies.py`` and the ``gtwy_browser_cookies`` collection; every document has
expired by then.

Every function swallows its errors: losing a stored login means the user signs in again,
which must never break a browsing turn.
"""

from datetime import UTC, datetime

from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

browserCookieModel = db["gtwy_browser_cookies"]


async def get_browser_cookies(scope_key: str) -> dict | None:
    """The stored document for this conversation, or None when absent or expired.

    Mongo's TTL monitor only sweeps about once a minute, so expiry is checked here
    rather than trusted to have happened already.
    """
    try:
        document = await with_timeout(browserCookieModel.find_one({"scope_key": scope_key}))
        if not document:
            return None
        expires_at = document.get("expires_at")
        if expires_at and expires_at.replace(tzinfo=expires_at.tzinfo or UTC) <= datetime.now(UTC):
            return None
        return document
    except Exception as error:
        logger.error(f"Error in get_browser_cookies for {scope_key}: {error!s}")
        return None


async def delete_browser_cookies(scope_key: str) -> bool:
    """Forget this conversation's old cookies once they live in its Browser Use profile."""
    try:
        await with_timeout(browserCookieModel.delete_one({"scope_key": scope_key}))
        return True
    except Exception as error:
        logger.error(f"Error in delete_browser_cookies for {scope_key}: {error!s}")
        return False
