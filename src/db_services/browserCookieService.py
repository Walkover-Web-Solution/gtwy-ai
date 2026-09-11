"""Durable store for a conversation's browser logins.

One document per conversation holds nothing but an encrypted cookie blob, so a user
who signed in yesterday is still signed in when the conversation opens a new tab.
Redis sits in front of this as a cache; Mongo is the source of truth.

Every function swallows its errors: losing a stored login means the user signs in
again, which must never break a browsing turn.
"""

from datetime import UTC, datetime, timedelta

from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

browserCookieModel = db["gtwy_browser_cookies"]


async def ensure_indexes() -> None:
    """Create the two indexes this collection needs. Idempotent, safe on every boot."""
    try:
        await with_timeout(browserCookieModel.create_index("scope_key", unique=True))
        # The deadline lives in the document, so changing the TTL setting needs no index rebuild.
        await with_timeout(browserCookieModel.create_index("expires_at", expireAfterSeconds=0))
        logger.info("Gtwy_Browser: cookie store indexes are in place")
    except Exception as error:
        logger.error(f"Error creating gtwy_browser_cookies indexes: {error!s}")


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


async def upsert_browser_cookies(scope_key: str, encrypted: str, cookie_count: int, ttl_seconds: int, meta: dict | None = None) -> bool:
    """Write this conversation's encrypted cookies, replacing whatever was there."""
    try:
        now = datetime.now(UTC)
        fields = {
            "scope_key": scope_key,
            "cookies": encrypted,
            "cookie_count": cookie_count,
            "version": 1,
            "updated_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
            **{key: value for key, value in (meta or {}).items() if value is not None},
        }
        await with_timeout(
            browserCookieModel.find_one_and_update({"scope_key": scope_key}, {"$set": fields}, upsert=True)
        )
        return True
    except Exception as error:
        logger.error(f"Error in upsert_browser_cookies for {scope_key}: {error!s}")
        return False
