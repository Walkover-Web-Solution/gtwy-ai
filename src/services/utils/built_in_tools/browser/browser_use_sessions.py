"""One Browser Use browser per conversation: start it, find it again, stop it when idle.

* A conversation gets a whole Chromium of its own. Its live view can only ever show its own
  pages, and one browser's crash or heavy page touches only that conversation.
* Redis keeps one record per conversation (``nd_gtwy_browser_bu_<key>``) with the browser id,
  its CDP and live URLs and the page in use, a set of every conversation key for the reaper
  (``nd_gtwy_browser_bu_index``), and one set per org (``nd_gtwy_browser_bu_org_<org>``) so an
  org can hold at most MAX_BROWSERS_PER_ORG browsers and cannot use up the account's limit.
* The browser's default context is used, never a new one: that is where the conversation's
  profile (its saved logins) is loaded. The profile is written back when the browser stops.
* Every browser goes through the residential proxy (India by default); the agent's browser
  settings (``gtwy_browser_settings.proxy_country``) pick another country or "none".
* Disconnecting does not stop a Browser Use browser and it bills while it runs, so the
  reaper stops browsers whose conversation went quiet, and any browser this environment
  started that no record points at.
"""

import asyncio
import json
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

from . import browser_use_client, legacy_cookies, profiles
from .browser_use_client import BrowserUseError, ProfileNotFound, TooManyBrowsers, redact
from .connection import BrowserConnectionError, connect, conversation_page, deny_downloads, disconnect
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
LOCK_PREFIX = "gtwy_browser_bu"
ORG_LOCK_PREFIX = "gtwy_browser_bu_org"
LOCK_TTL = 30
LOCK_WAIT_SECONDS = 5.0
# Waiting on the account's browser limit: roughly how long until the reaper frees one.
LIMIT_RETRY_SECONDS = 30
# Browsers one org may have open at once, so one busy org cannot use up the account's limit
# (10 at once on the free plan, 50 after a $100 top-up). 0 turns the org limit off.
MAX_BROWSERS_PER_ORG = 20


class BrowserLimitReached(BrowserBusy):
    """No browser can start: the org has its share open, or the account is at its plan's limit."""


# ---------------------------------------------------------------- records in Redis


def _record_key(tkey: str) -> str:
    return f"{RECORD_PREFIX}{tkey}"


def _org_key(org_id) -> str:
    return f"{ORG_PREFIX}{org_id}"


def _org_of(tkey: str, record: dict | None = None) -> str:
    return str((record or {}).get("org_id") or tkey.split(":", 1)[0])


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
    """Browser Use stops a browser at its ``timeoutAt`` on its own; such a record is dead."""
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


async def _stop_and_forget(tkey: str, record: dict, reason: str) -> bool:
    """Stop a conversation's browser (its profile is saved as it stops) and forget it."""
    try:
        await browser_use_client.stop_browser(record["browser_id"])
    except BrowserUseError as exc:
        logger.warning(f"Gtwy_Browser: could not stop browser {redact(record['browser_id'])} of {tkey}: {exc}")
        return False
    await _forget(tkey, record)
    logger.info(f"Gtwy_Browser: stopped browser {redact(record['browser_id'])} of thread {tkey} ({reason})")
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
    browser = await connect(record["browser_id"], record["cdp_url"])
    page, target_id = await conversation_page(browser, record.get("target_id"))
    _touch(record, target_id=target_id)
    await _save(tkey, record)
    return page, record


async def _create(tkey: str, org_id, bridge_id, settings: dict | None) -> tuple[dict, bool]:
    """Start the hosted browser for this conversation. Returns (record, profile just created).
    The caller holds the conversation's lock and the org's lock."""
    profile_id, new_profile = await profiles.profile_for(tkey, org_id, bridge_id)
    country = browser_use_client.proxy_country((settings or {}).get("proxy_country"))
    metadata = {"gtwy_env": Config.ENVIRONMENT or "unknown", "gtwy_org": org_id, "gtwy_proxy": country or "none"}
    try:
        try:
            view = await browser_use_client.create_browser(profile_id, metadata, country)
        except ProfileNotFound:
            logger.warning(f"Gtwy_Browser: profile {redact(profile_id)} of {tkey} is gone; starting with a new one")
            profile_id = await profiles.new_profile(tkey, org_id, bridge_id)
            new_profile = profile_id is not None
            view = await browser_use_client.create_browser(profile_id, metadata, country)
    except TooManyBrowsers as exc:
        logger.warning(f"Gtwy_Browser: browser use account is at its browser limit ({exc})")
        raise BrowserLimitReached(
            f"the browser service is running as many browsers as its plan allows; retry in {LIMIT_RETRY_SECONDS} seconds",
            LIMIT_RETRY_SECONDS,
        ) from exc

    record = _touch(
        {
            "browser_id": view["id"],
            "cdp_url": view["cdpUrl"],
            "live_url": view.get("liveUrl"),
            "timeout_at": view.get("timeoutAt"),
            "profile_id": profile_id,
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
    return record, new_profile


async def _start(tkey: str, org_id, bridge_id, settings: dict | None):
    """Start this conversation's browser. Holds the conversation's lock so two calls of the same
    conversation never start two browsers, and the org's lock so the org stays within its limit."""
    if not await _acquire(f"{LOCK_PREFIX}_{tkey}"):
        raise BrowserStarting(5)
    try:
        record = await get_record(tkey)
        if record and not is_expired(record):
            return await _reuse(tkey, record)

        org = _org_of(tkey, {"org_id": org_id})
        if not await _acquire(f"{ORG_LOCK_PREFIX}_{org}"):
            raise BrowserLimitReached("another browser of your organization is starting; retry in 5 seconds", 5)
        try:
            await _make_room_in_org(org, tkey)
            record, new_profile = await _create(tkey, org_id, bridge_id, settings)
        finally:
            await release_lock(f"{ORG_LOCK_PREFIX}_{org}")

        browser = await connect(record["browser_id"], record["cdp_url"], fresh=True)
        await deny_downloads(browser)
        if new_profile:
            await legacy_cookies.import_once(browser, tkey)
        page, target_id = await conversation_page(browser, None)
        _touch(record, target_id=target_id)
        await _save(tkey, record)
        return page, record
    finally:
        await release_lock(f"{LOCK_PREFIX}_{tkey}")


async def open_or_reuse_tab(tkey: str, org_id, bridge_id, settings: dict | None = None):
    """Return (page, record) for this conversation, starting its browser if needed.

    ``settings`` are the agent's browser settings (``proxy_country``); they apply when a browser
    starts. The record carries the browser id, its live URL and the page in use.
    """
    record = await get_record(tkey)
    if record and not is_expired(record):
        try:
            return await _reuse(tkey, record)
        except BrowserConnectionError:
            # A browser that stopped (timeout, reaper, stopped from the dashboard) is replaced. One
            # that still runs but did not answer is left alone: the model is told to retry.
            view = await browser_use_client.get_browser(record["browser_id"])
            if view and view.get("status") == "active":
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
    return await _start(tkey, org_id, bridge_id, settings)


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
    if not record or is_expired(record) or not record.get("live_url"):
        return None
    try:
        view = await browser_use_client.get_browser(record["browser_id"])
    except BrowserUseError as exc:
        logger.warning(f"Gtwy_Browser: live view could not check browser {redact(record['browser_id'])} ({exc})")
        return record["live_url"]
    if not view or view.get("status") != "active":
        await _forget(tkey, record)
        return None
    return view.get("liveUrl") or record["live_url"]


async def reap_idle_browsers() -> None:
    """Stop the browsers of quiet conversations, then any running browser of this environment
    that no conversation points at (a record lost to a Redis expiry or a crash mid-start).
    The caller holds the reaper lock."""
    now = time.time()
    known: set[str] = set()
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
            known.add(record["browser_id"])
            continue
        if not await _stop_and_forget(tkey, record, reason="idle"):
            known.add(record["browser_id"])

    try:
        running = await browser_use_client.list_active_browsers([f"gtwy_env={Config.ENVIRONMENT or 'unknown'}"])
    except BrowserUseError as exc:
        logger.warning(f"Gtwy_Browser: reaper could not list browser use browsers ({exc})")
        return
    for view in running:
        started = _parse_time(view.get("startedAt")) or now
        # A browser started in the last few minutes may belong to a call that has not saved its
        # record yet, so only older strays are stopped.
        if view.get("id") in known or now - started < IDLE_TIMEOUT_SECONDS:
            continue
        try:
            await browser_use_client.stop_browser(view["id"])
            logger.warning(
                f"Gtwy_Browser: reaper stopped stray browser {redact(view['id'])} that no conversation owned"
            )
        except BrowserUseError as exc:
            logger.warning(f"Gtwy_Browser: reaper could not stop stray browser {redact(view.get('id'))}: {exc}")
