"""Browser Use profiles: where a conversation's logins live.

The browser's own state (cookies and local storage) is saved into a profile on Browser Use's
side when the browser stops, and the next browser started with that profile id opens signed
in. gtwy stores only the id, per conversation, in ``gtwy_browser_profiles``.

A profile failure never blocks browsing: without a profile the browser simply starts
logged out.
"""

from globals import logger

from . import browser_use_client
from .browser_use_client import BrowserUseError

PROVIDER = "browser_use"


async def profile_for(tkey: str, org_id=None, bridge_id=None) -> tuple[str | None, bool]:
    """(profile id, just created) for this conversation, creating the profile on first use."""
    from src.db_services.browserProfileService import get_browser_profile, touch_browser_profile

    document = await get_browser_profile(tkey, PROVIDER)
    if document and document.get("profile_id"):
        await touch_browser_profile(tkey, PROVIDER)
        return document["profile_id"], False
    profile_id = await new_profile(tkey, org_id, bridge_id)
    return profile_id, profile_id is not None


async def new_profile(tkey: str, org_id=None, bridge_id=None) -> str | None:
    """Create a fresh profile for this conversation and remember it, replacing any old id."""
    from src.db_services.browserProfileService import upsert_browser_profile

    try:
        profile_id = await browser_use_client.create_profile(
            name=f"gtwy {tkey}", user_id=str(org_id) if org_id else None
        )
    except BrowserUseError as exc:
        logger.error(f"Gtwy_Browser: could not create a browser use profile for {tkey}: {exc}")
        return None
    if not await upsert_browser_profile(tkey, PROVIDER, profile_id, {"org_id": org_id, "bridge_id": bridge_id}):
        logger.error(f"Gtwy_Browser: could not store profile {browser_use_client.redact(profile_id)} for {tkey}")
    return profile_id
