"""One hosted browser per conversation, on the agent's browser service: start it, find it
again, stop it when idle.

* A conversation gets a whole Chromium of its own on the service its agent picked
  (``providers.py``). Its live view can only ever show its own pages, and one browser's crash
  or heavy page touches only that conversation.
* Redis keeps one record per conversation (``nd_gtwy_browser_bu_<key>``) with the service, the
  browser id, its CDP and live URLs and the page in use, a set of every conversation key for the
  reaper (``nd_gtwy_browser_bu_index``), and one set per org (``nd_gtwy_browser_bu_org_<org>``)
  so an org can hold at most MAX_BROWSERS_PER_ORG browsers across all services and cannot use
  up an account's limit. A record without a service predates the choice and is Browser Use.
* A running browser keeps its service until it stops; a changed choice applies to the next
  browser. Switching on the spot would make two agents sharing a thread (a transfer) stop each
  other's browsers on every call.
* The browser's default context is used, never a new one: that is where the conversation's
  profile (its saved logins on that service) is loaded. The profile is written back when the
  browser stops, and some services need a few seconds before the next browser may load it.
* Every browser goes through a residential proxy (India by default); the agent's browser
  settings (``gtwy_browser_settings.proxy_country``) pick another country or "none".
* Disconnecting does not stop a hosted browser and it bills while it runs, so the reaper stops
  browsers whose conversation went quiet, and any browser this environment started that no
  record points at.
"""

import asyncio
import json
import math
import time
from datetime import UTC, datetime

from config import Config
from globals import logger
from src.configs.constant import redis_keys
from src.services.cache_service import (
    acquire_lock,
    delete_in_cache,
    find_in_cache,
    release_lock,
    sadd_in_cache,
    smembers_in_cache,
    srem_in_cache,
    store_in_cache,
)

from . import legacy_cookies, profiles, providers
from .connection import BrowserConnectionError, connect, conversation_page, deny_downloads, disconnect
from .provider_base import BrowserServiceError, ProfileBusy, ProfileNotFound, TooManyBrowsers, proxy_country, redact
from .session_store import (
    IDLE_TIMEOUT_SECONDS,
    RECORD_TTL_SECONDS,
    BrowserBusy,
    BrowserStarting,
    clear_thread_state,
    tab_idle_limit,
)

RECORD_PREFIX = redis_keys["gtwy_browser_bu_"]
INDEX_KEY = redis_keys["gtwy_browser_bu_index"]
ORG_PREFIX = redis_keys["gtwy_browser_bu_org_"]
SAVING_PREFIX = redis_keys["gtwy_browser_saving_"]
LOCK_PREFIX = "gtwy_browser_bu"
ORG_LOCK_PREFIX = "gtwy_browser_bu_org"
LOCK_TTL = 30
LOCK_WAIT_SECONDS = 5.0
# Waiting on a service account's browser limit: roughly how long until the reaper frees one.
LIMIT_RETRY_SECONDS = 30
# A profile the last browser is still saving: wait this long once before telling the user.
PROFILE_BUSY_RETRY_SECONDS = 1.5
PROFILE_BUSY_RETRY_IN = 5
# Browsers one org may have open at once, across every service, so one busy org cannot use up
# an account's limit. 0 turns the org limit off.
MAX_BROWSERS_PER_ORG = 20


class BrowserLimitReached(BrowserBusy):
    """No browser can start: the org has its share open, or the account is at its plan's limit."""


# ---------------------------------------------------------------- records in Redis


def _record_key(tkey: str) -> str:
    return f"{RECORD_PREFIX}{tkey}"


def _org_key(org_id) -> str:
    return f"{ORG_PREFIX}{org_id}"


def _saving_key(provider: str, tkey: str) -> str:
    return f"{SAVING_PREFIX}{provider}_{tkey}"


def _org_of(tkey: str, record: dict | None = None) -> str:
    return str((record or {}).get("org_id") or tkey.split(":", 1)[0])


def _provider_of(record: dict) -> str:
    return record.get("provider") or providers.DEFAULT_PROVIDER


def _client_of(record: dict):
    """The client of the record's service, or None when that service is not set up any more."""
    client = providers.get(_provider_of(record))
    return client if client is not None and client.is_configured() else None


async def get_record(tkey: str) -> dict | None:
    raw = await find_in_cache(_record_key(tkey))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("browser_id") else None


async def _save(tkey: str, record: dict) -> None:
    await store_in_cache(_record_key(tkey), record, ttl=RECORD_TTL_SECONDS)


def _touch(record: dict, **updates) -> dict:
    record["last_used_at"] = time.time()
    record.update(updates)
    return record


def _idle_for(record: dict, now: float) -> float:
    return now - float(record.get("last_used_at") or 0)


def _parse_time(value) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def is_expired(record: dict, now: float | None = None) -> bool:
    """A service stops a browser at its ``timeout_at`` on its own; such a record is dead.
    A service without an absolute lifetime (Kernel) has no ``timeout_at``."""
    deadline = _parse_time(record.get("timeout_at"))
    return deadline is not None and (now or time.time()) >= deadline


async def _acquire(name: str) -> bool:
    deadline = asyncio.get_event_loop().time() + LOCK_WAIT_SECONDS
    while True:
        if await acquire_lock(name, ttl=LOCK_TTL):
            return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


async def _forget(tkey: str, record: dict | None) -> None:
    await delete_in_cache(_record_key(tkey))
    await srem_in_cache(INDEX_KEY, tkey)
    await srem_in_cache(_org_key(_org_of(tkey, record)), tkey)
    await clear_thread_state(tkey)
    await disconnect((record or {}).get("browser_id"))


async def _mark_saving(tkey: str, record: dict, client) -> None:
    """Remember that the stopped browser's profile is being saved, for the next start."""
    seconds = getattr(client, "PROFILE_SAVE_SECONDS", 0)
    if seconds > 0 and record.get("profile_id"):
        await store_in_cache(_saving_key(client.NAME, tkey), {"until": time.time() + seconds}, ttl=math.ceil(seconds))


async def _wait_for_profile_save(tkey: str, provider: str) -> None:
    """Let a profile the last browser is saving finish before the next browser loads it."""
    raw = await find_in_cache(_saving_key(provider, tkey))
    if not raw:
        return
    try:
        remaining = float(json.loads(raw).get("until") or 0) - time.time()
    except (TypeError, ValueError, AttributeError):
        return
    if remaining > 0:
        await asyncio.sleep(min(remaining, PROFILE_BUSY_RETRY_IN))


async def _stop_and_forget(tkey: str, record: dict, reason: str) -> bool:
    """Stop a conversation's browser (its profile is saved as it stops) and forget it."""
    client = _client_of(record)
    if client is None:
        # Its service's key was removed from this server: the browser cannot be stopped from here
        # any more and ends at its own timeout.
        logger.warning(
            f"Gtwy_Browser: {_provider_of(record)} is not set up any more; forgetting browser "
            f"{redact(record['browser_id'])} of {tkey}"
        )
        await _forget(tkey, record)
        return True
    try:
        await client.stop_browser(record["browser_id"])
    except BrowserServiceError as exc:
        logger.warning(f"Gtwy_Browser: could not stop browser {redact(record['browser_id'])} of {tkey}: {exc}")
        return False
    await _mark_saving(tkey, record, client)
    await _forget(tkey, record)
    logger.info(
        f"Gtwy_Browser: stopped {client.LABEL} browser {redact(record['browser_id'])} of thread {tkey} ({reason})"
    )
    return True


# ---------------------------------------------------------------- per-org limit


async def _make_room_in_org(org_id: str, tkey: str) -> None:
    """Make sure the org may open one more browser. The caller holds the org lock.

    Members without a live record are dropped. When the org is at its limit, its quietest
    browser that has been idle past its limit is stopped to free a slot; when none is idle,
    BrowserLimitReached says when the first one will be.
    """
    limit = MAX_BROWSERS_PER_ORG
    if limit <= 0:
        return
    now = time.time()
    open_records = []
    for key in await smembers_in_cache(_org_key(org_id)):
        if key == tkey:
            continue
        record = await get_record(key)
        if not record or is_expired(record, now):
            await srem_in_cache(_org_key(org_id), key)
            continue
        open_records.append((key, record))
    if len(open_records) < limit:
        return

    idle = sorted(
        ((key, record) for key, record in open_records if _idle_for(record, now) > tab_idle_limit(record)),
        key=lambda item: float(item[1].get("last_used_at") or 0),
    )
    open_count = len(open_records)
    for key, record in idle:
        if await _stop_and_forget(key, record, reason="freed a slot for another conversation of the org"):
            open_count -= 1
            if open_count < limit:
                return

    wait = max(1, int(min(tab_idle_limit(r) - _idle_for(r, now) for _, r in open_records)))
    logger.info(f"Gtwy_Browser: org {org_id} has {open_count} browsers open (limit {limit}); thread {tkey} waits")
    raise BrowserLimitReached(
        f"your organization already has {open_count} browsers open, the most allowed at once; retry in {wait} seconds",
        wait,
    )


# ---------------------------------------------------------------- the entry points


async def _reuse(tkey: str, record: dict):
    client = _client_of(record)
    browser = await connect(record["browser_id"], client.connect_url(record))
    page, target_id = await conversation_page(browser, record.get("target_id"))
    _touch(record, target_id=target_id)
    await _save(tkey, record)
    return page, record


async def _start_browser(client, tkey: str, provider: str, profile_id, metadata: dict, country, org_id, bridge_id):
    """The service's new browser, with this conversation's profile when it has one."""
    try:
        return await client.create_browser(profile_id, metadata, country)
    except ProfileNotFound:
        logger.warning(
            f"Gtwy_Browser: {client.LABEL} profile {redact(profile_id)} of {tkey} is gone; starting with a new one"
        )
        profile_id = await profiles.new_profile(tkey, provider, client, org_id, bridge_id)
        return await client.create_browser(profile_id, metadata, country)
    except ProfileBusy:
        # The last browser is still saving this profile; it usually takes a second or two.
        await asyncio.sleep(PROFILE_BUSY_RETRY_SECONDS)
        try:
            return await client.create_browser(profile_id, metadata, country)
        except ProfileBusy as exc:
            raise BrowserBusy(
                f"saving your logins from the last browser; retry in {PROFILE_BUSY_RETRY_IN} seconds",
                PROFILE_BUSY_RETRY_IN,
            ) from exc


async def _create(tkey: str, provider: str, org_id, bridge_id, settings: dict | None) -> tuple[dict, bool]:
    """Start the hosted browser for this conversation. Returns (record, first browser of this
    conversation on this service). The caller holds the conversation's lock and the org's lock."""
    client = providers.get(provider)
    profile_id, first = await profiles.profile_for(tkey, provider, client, org_id, bridge_id)
    country = proxy_country((settings or {}).get("proxy_country"))
    metadata = {"gtwy_env": Config.ENVIRONMENT or "unknown", "gtwy_org": org_id, "gtwy_proxy": country or "none"}
    try:
        view = await _start_browser(client, tkey, provider, profile_id, metadata, country, org_id, bridge_id)
    except TooManyBrowsers as exc:
        logger.warning(f"Gtwy_Browser: {client.LABEL} account is at its browser limit ({exc})")
        raise BrowserLimitReached(
            f"the {client.LABEL} browser service is running as many browsers as its plan allows; "
            f"retry in {LIMIT_RETRY_SECONDS} seconds",
            LIMIT_RETRY_SECONDS,
        ) from exc

    if view.get("profile_id") and view["profile_id"] != profile_id:
        # The service created the profile with this browser (Steel), or replaced a lost one.
        await profiles.remember(tkey, provider, view["profile_id"], org_id, bridge_id)
    record = _touch(
        {
            "provider": provider,
            "browser_id": view["id"],
            "cdp_url": view["cdp_url"],
            "live_url": view.get("live_url"),
            "timeout_at": view.get("timeout_at"),
            "profile_id": view.get("profile_id") or profile_id,
            "proxy_country": country,
            "target_id": None,
            "handoff_active": False,
            "org_id": org_id,
            "bridge_id": bridge_id,
            "created_at": time.time(),
        }
    )
    # Saved before connecting: if the connect fails, the next call finds this browser and
    # retries instead of starting (and paying for) a second one.
    await _save(tkey, record)
    await sadd_in_cache(INDEX_KEY, tkey)
    await sadd_in_cache(_org_key(_org_of(tkey, record)), tkey)
    return record, first


async def _start(tkey: str, provider: str, org_id, bridge_id, settings: dict | None):
    """Start this conversation's browser. Holds the conversation's lock so two calls of the same
    conversation never start two browsers, and the org's lock so the org stays within its limit."""
    if not await _acquire(f"{LOCK_PREFIX}_{tkey}"):
        raise BrowserStarting(5)
    try:
        record = await get_record(tkey)
        if record and not is_expired(record) and _client_of(record) is not None:
            return await _reuse(tkey, record)

        await _wait_for_profile_save(tkey, provider)
        org = _org_of(tkey, {"org_id": org_id})
        if not await _acquire(f"{ORG_LOCK_PREFIX}_{org}"):
            raise BrowserLimitReached("another browser of your organization is starting; retry in 5 seconds", 5)
        try:
            await _make_room_in_org(org, tkey)
            record, first = await _create(tkey, provider, org_id, bridge_id, settings)
        finally:
            await release_lock(f"{ORG_LOCK_PREFIX}_{org}")

        client = providers.get(provider)
        browser = await connect(record["browser_id"], client.connect_url(record), fresh=True)
        await deny_downloads(browser)
        if first:
            await legacy_cookies.import_once(browser, tkey)
        page, target_id = await conversation_page(browser, None)
        _touch(record, target_id=target_id)
        await _save(tkey, record)
        return page, record
    finally:
        await release_lock(f"{LOCK_PREFIX}_{tkey}")


async def open_or_reuse_tab(tkey: str, org_id, bridge_id, settings: dict | None = None, provider: str | None = None):
    """Return (page, record) for this conversation, starting its browser if needed.

    ``provider`` is the service the agent picked (already checked to be set up); it and
    ``settings`` (``proxy_country``) apply when a browser starts. A running browser is reused
    whatever its service. The record carries the service, the browser id and the page in use.
    """
    provider = provider or providers.DEFAULT_PROVIDER
    record = await get_record(tkey)
    if record and _client_of(record) is None:
        logger.warning(
            f"Gtwy_Browser: {_provider_of(record)} is not set up any more; forgetting browser "
            f"{redact(record['browser_id'])} of {tkey}"
        )
        await _forget(tkey, record)
        record = None
    if record and not is_expired(record):
        try:
            return await _reuse(tkey, record)
        except BrowserConnectionError:
            # A browser that stopped (timeout, reaper, stopped from the dashboard) is replaced. One
            # that still runs but did not answer is left alone: the model is told to retry.
            view = await _client_of(record).get_browser(record["browser_id"])
            if view and view.get("active"):
                raise
            logger.info(
                f"Gtwy_Browser: browser {redact(record['browser_id'])} of {tkey} has stopped; starting a new one"
            )
            await _forget(tkey, record)
    elif record:
        logger.info(
            f"Gtwy_Browser: browser {redact(record['browser_id'])} of {tkey} reached its timeout; starting a new one"
        )
        await _forget(tkey, record)
    return await _start(tkey, provider, org_id, bridge_id, settings)


async def set_conversation_page(tkey: str, target_id: str | None) -> None:
    """Point the conversation at another tab of its browser, e.g. one that a click opened."""
    record = await get_record(tkey)
    if record and target_id:
        _touch(record, target_id=target_id)
        await _save(tkey, record)


async def mark_handoff(tkey: str, active: bool) -> None:
    """Mirror the handoff flag onto the record so the reaper waits longer for a login."""
    record = await get_record(tkey)
    if record:
        _touch(record, handoff_active=active)
        await _save(tkey, record)


async def release_tab(tkey: str, reason: str = "") -> None:
    """Stop this conversation's browser (its profile is saved as it stops) and forget it."""
    record = await get_record(tkey)
    if record and not is_expired(record):
        if await _stop_and_forget(tkey, record, reason=reason):
            return
    await _forget(tkey, record)


async def drop_connection(tab: dict | None) -> None:
    """Forget this pod's connection to a conversation's browser after it was lost."""
    await disconnect((tab or {}).get("browser_id"))


async def live_target(tkey: str) -> str | None:
    """The live view URL of this conversation's running browser, or None when it has none."""
    record = await get_record(tkey)
    if not record or is_expired(record):
        return None
    client = _client_of(record)
    if client is None:
        return None
    try:
        view = await client.get_browser(record["browser_id"])
    except BrowserServiceError as exc:
        logger.warning(f"Gtwy_Browser: live view could not check browser {redact(record['browser_id'])} ({exc})")
        view = None
    else:
        if not view or not view.get("active"):
            await _forget(tkey, record)
            return None
    return await client.live_view_url(record, view)


async def reap_idle_browsers() -> None:
    """Stop the browsers of quiet conversations, then any running browser of this environment
    that no conversation points at (a record lost to a Redis expiry or a crash mid-start).
    The caller holds the reaper lock."""
    now = time.time()
    known: set[tuple[str, str]] = set()
    for tkey in await smembers_in_cache(INDEX_KEY):
        record = await get_record(tkey)
        if not record:
            await srem_in_cache(INDEX_KEY, tkey)
            await srem_in_cache(_org_key(_org_of(tkey)), tkey)
            continue
        if is_expired(record, now):
            await _forget(tkey, record)
            logger.info(f"Gtwy_Browser: reaper forgot browser {redact(record['browser_id'])} of {tkey}: timed out")
            continue
        if _idle_for(record, now) <= tab_idle_limit(record):
            known.add((_provider_of(record), record["browser_id"]))
            continue
        if not await _stop_and_forget(tkey, record, reason="idle"):
            known.add((_provider_of(record), record["browser_id"]))

    env = Config.ENVIRONMENT or "unknown"
    for client in providers.configured():
        try:
            running = await client.list_active_browsers(env)
        except BrowserServiceError as exc:
            logger.warning(f"Gtwy_Browser: reaper could not list {client.LABEL} browsers ({exc})")
            continue
        # None: the service cannot tell this environment's browsers apart, so none are touched.
        for view in running or []:
            started = _parse_time(view.get("started_at")) or now
            # A browser started in the last few minutes may belong to a call that has not saved its
            # record yet, so only older strays are stopped.
            if not view.get("id") or (client.NAME, view["id"]) in known or now - started < IDLE_TIMEOUT_SECONDS:
                continue
            try:
                await client.stop_browser(view["id"])
                logger.warning(
                    f"Gtwy_Browser: reaper stopped stray {client.LABEL} browser {redact(view['id'])} "
                    "that no conversation owned"
                )
            except BrowserServiceError as exc:
                logger.warning(f"Gtwy_Browser: reaper could not stop stray browser {redact(view.get('id'))}: {exc}")
