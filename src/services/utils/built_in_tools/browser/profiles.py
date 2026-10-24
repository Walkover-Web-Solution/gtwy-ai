"""Saved-login profiles: where a conversation's logins live on each browser service.

The browser's own state (cookies and local storage) is saved into a profile on the service's
side when the browser stops, and the next browser started with that profile opens signed in.
gtwy stores only the id, per conversation and per service, in ``gtwy_browser_profiles``: logins
made on one service are not on another.

Services create profiles differently (an API call, a name, or the first session), so a client's
``create_profile`` may return None and the id then comes back with the first browser.

A profile failure never blocks browsing: without a profile the browser simply starts
logged out.
"""

from globals import logger

from .provider_base import BrowserServiceError, redact


async def profile_for(tkey: str, provider: str, client, org_id=None, bridge_id=None) -> tuple[str | None, bool]:
    """(profile id, first browser of this conversation on this service).

    The id is None when the service creates the profile with the first browser.
    """
    from src.db_services.browserProfileService import get_browser_profile, touch_browser_profile

    document = await get_browser_profile(tkey, provider)
    if document and document.get("profile_id"):
        await touch_browser_profile(tkey, provider)
        return document["profile_id"], False
    return await new_profile(tkey, provider, client, org_id, bridge_id), document is None


async def new_profile(tkey: str, provider: str, client, org_id=None, bridge_id=None) -> str | None:
    """Create a fresh profile for this conversation on this service and remember it."""
    try:
        profile_id = await client.create_profile(tkey, org_id)
    except BrowserServiceError as exc:
        logger.error(f"Gtwy_Browser: could not create a {client.LABEL} profile for {tkey}: {exc}")
        return None
    if profile_id:
        await remember(tkey, provider, profile_id, org_id, bridge_id)
    return profile_id


async def remember(tkey: str, provider: str, profile_id: str | None, org_id=None, bridge_id=None) -> None:
    """Store which profile holds this conversation's logins on this service."""
    from src.db_services.browserProfileService import upsert_browser_profile

    if not profile_id:
        return
    if not await upsert_browser_profile(tkey, provider, profile_id, {"org_id": org_id, "bridge_id": bridge_id}):
        logger.error(f"Gtwy_Browser: could not store {provider} profile {redact(profile_id)} for {tkey}")
