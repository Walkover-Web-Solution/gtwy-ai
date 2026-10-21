"""Which Browser Use profile holds a conversation's browser logins.

With the Browser Use backend the logins themselves (cookies, local storage) live in a
Browser Use profile, saved there when the browser stops. gtwy keeps only the profile id,
one document per conversation, so a returning conversation starts its new browser
already signed in.

Every function swallows its errors: losing the mapping means the user signs in again,
which must never break a browsing turn.
"""

from datetime import UTC, datetime

from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

browserProfileModel = db["gtwy_browser_profiles"]


async def ensure_indexes() -> None:
    """One profile per conversation and provider. Idempotent, safe on every boot."""
    try:
        await with_timeout(browserProfileModel.create_index([("scope_key", 1), ("provider", 1)], unique=True))
        logger.info("Gtwy_Browser: profile store indexes are in place")
    except Exception as error:
        logger.error(f"Error creating gtwy_browser_profiles indexes: {error!s}")


async def get_browser_profile(scope_key: str, provider: str) -> dict | None:
    try:
        return await with_timeout(browserProfileModel.find_one({"scope_key": scope_key, "provider": provider}))
    except Exception as error:
        logger.error(f"Error in get_browser_profile for {scope_key}: {error!s}")
        return None


async def upsert_browser_profile(scope_key: str, provider: str, profile_id: str, meta: dict | None = None) -> bool:
    try:
        now = datetime.now(UTC)
        fields = {
            "scope_key": scope_key,
            "provider": provider,
            "profile_id": profile_id,
            "updated_at": now,
            **{key: value for key, value in (meta or {}).items() if value is not None},
        }
        await with_timeout(
            browserProfileModel.find_one_and_update(
                {"scope_key": scope_key, "provider": provider},
                {"$set": fields, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        )
        return True
    except Exception as error:
        logger.error(f"Error in upsert_browser_profile for {scope_key}: {error!s}")
        return False


async def touch_browser_profile(scope_key: str, provider: str) -> None:
    try:
        await with_timeout(
            browserProfileModel.update_one(
                {"scope_key": scope_key, "provider": provider}, {"$set": {"last_used_at": datetime.now(UTC)}}
            )
        )
    except Exception as error:
        logger.error(f"Error in touch_browser_profile for {scope_key}: {error!s}")
