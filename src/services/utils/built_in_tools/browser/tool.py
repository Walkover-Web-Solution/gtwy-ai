"""Entry point for the built-in Gtwy_Browser tool.

``call_gtwy_browser(args, ctx)`` follows the same contract as every other gtwy tool
coroutine: it always returns ``{"response", "metadata", "status": 0|1}`` and never
raises. Each conversation drives its own tab, with its own cookies, inside one shared
Chrome, so two conversations can be signed in to the same site as different people.
"""

import asyncio
import time

from globals import logger

from . import steel_client
from .actions import (
    BrowserActionError,
    StaleRefError,
    do_back,
    do_click,
    do_navigate,
    do_press,
    do_scroll,
    do_snapshot,
    do_type,
    looks_like_login_page,
)
from .connection import BrowserConnectionError, is_connection_lost_error, is_timeout_error, reset_connection
from .schema import BROWSER_ACTIONS
from .security import UrlBlocked, ensure_url_allowed, wrap_untrusted
from .session_store import BrowserBusy, get_thread_state, save_thread_state, thread_key
from .steel_client import SteelError
from .tabs import mark_handoff, open_or_reuse_tab, release_tab

TOOL_TIMEOUT_SECONDS = 60
HANDOFF_INSTRUCTIONS = (
    "Reply to the user now: include the live_url as a clickable link, tell them to open it, "
    "complete the action there (log in, solve the CAPTCHA, enter the OTP), and reply 'done' "
    "when finished. Do not take any other browser action in this turn. When the user replies, "
    "call snapshot and continue the original task."
)


def _err(message: str, **extra) -> dict:
    return {"response": {"error": message, **extra}, "metadata": {"type": "function"}, "status": 0}


def _ok(response: dict, **meta) -> dict:
    return {"response": response, "metadata": {"type": "function", "browser": meta}, "status": 1}


async def call_gtwy_browser(args: dict | None, ctx: dict | None) -> dict:
    try:
        return await asyncio.wait_for(_run(args or {}, ctx or {}), timeout=TOOL_TIMEOUT_SECONDS)
    except TimeoutError:
        return _err(f"browser action timed out after {TOOL_TIMEOUT_SECONDS}s; try snapshot again")
    except Exception as exc:  # the tool loop expects a dict, never an exception
        logger.error(f"Gtwy_Browser: unexpected failure: {exc.__class__.__name__}: {exc}")
        return _err(f"browser tool failed: {exc.__class__.__name__}")


async def _run(args: dict, ctx: dict) -> dict:
    if not steel_client.is_configured():
        return _err("browser tool is not configured on this server (STEEL_API_URL missing)")

    action = (args.get("action") or "").strip()
    if action not in BROWSER_ACTIONS:
        return _err(f"unknown action '{action}'. Valid actions: {', '.join(BROWSER_ACTIONS)}")

    tkey = thread_key(ctx.get("org_id"), ctx.get("thread_id"), ctx.get("sub_thread_id"))

    if action == "navigate":
        # Check the URL before claiming a tab so a blocked URL never opens a browser.
        try:
            await ensure_url_allowed(args.get("url"))
        except UrlBlocked as exc:
            return _err(f"url not allowed: {exc}")

    try:
        browser, page, registry, tab = await open_or_reuse_tab(tkey, ctx.get("org_id"), ctx.get("bridge_id"))
    except BrowserBusy as busy:
        return _err(str(busy), retry_in_seconds=busy.retry_in)
    except SteelError as exc:
        logger.error(f"Gtwy_Browser: steel error while opening a tab: {exc}")
        return _err("browser backend is unavailable right now; try again later")
    except BrowserConnectionError as exc:
        logger.warning(f"Gtwy_Browser: connection error while opening a tab: {exc}")
        await reset_connection()
        return _err("could not reach the browser; try again in a moment")

    live_url = steel_client.live_view_url(registry.get("debug_url"), tab.get("target_id"))
    state = await get_thread_state(tkey)
    state["target_id"] = tab.get("target_id")

    # While the user is acting in the live view we take no snapshots, so nothing they type can
    # land in the transcript. A new user message (new message_id) or a navigate ends the handoff.
    if state.get("handoff_active"):
        user_replied = ctx.get("message_id") and ctx.get("message_id") != state.get("handoff_message_id")
        if action == "navigate" or user_replied:
            state.update(handoff_active=False, handoff_message=None, handoff_message_id=None, handoff_started_at=None)
            await save_thread_state(tkey, state)
            await mark_handoff(tkey, False)
        elif action != "request_user_action":
            return _err(
                "user is interacting with the browser; wait for the user to confirm, then call snapshot",
                live_url=live_url,
            )

    if action == "request_user_action":
        return await _handoff(args, ctx, tkey, live_url, state)

    try:
        response, new_ref_map = await _dispatch(action, args, page, state)
    except StaleRefError as exc:
        return _err(str(exc))
    except UrlBlocked as exc:
        return _err(f"url not allowed: {exc}")
    except BrowserActionError as exc:
        return _err(str(exc))
    except Exception as exc:
        if is_connection_lost_error(exc):
            logger.warning(f"Gtwy_Browser: tab lost for thread {tkey}: {exc.__class__.__name__}")
            await reset_connection()
            await release_tab(tkey, reason="tab lost")
            return _err("the browser tab was lost; call navigate again to start over")
        if is_timeout_error(exc):
            return _err("timeout: the page did not respond in time; call snapshot to see its current state")
        logger.error(f"Gtwy_Browser: action {action} failed: {exc.__class__.__name__}: {exc}")
        return _err(f"browser action failed: {exc.__class__.__name__}")

    if new_ref_map is not None:
        state["ref_map"] = new_ref_map
        state["snapshot_seq"] = int(state.get("snapshot_seq") or 0) + 1
    state["last_url"] = page.url

    # A page asking the user to sign in starts the handoff on its own, so the model never has to
    # remember to call request_user_action and never tries to type credentials.
    if new_ref_map is not None and await looks_like_login_page(page):
        await _begin_handoff(ctx, tkey, state, "This page requires you to log in.")
        await _emit_handoff(ctx, live_url, state["handoff_message"])
        response.update(login_required=True, live_url=live_url, instructions=HANDOFF_INSTRUCTIONS)

    await save_thread_state(tkey, state)

    if response.get("snapshot"):
        response["snapshot"] = wrap_untrusted(response["snapshot"])
    if response.get("title"):
        response["title"] = wrap_untrusted(response["title"])

    return _ok(response, action=action, url=page.url, tab=(tab.get("target_id") or "")[-6:])


async def _dispatch(action: str, args: dict, page, state: dict):
    ref_map = state.get("ref_map") or {}
    if action == "navigate":
        return await do_navigate(page, args.get("url"))
    if action == "snapshot":
        return await do_snapshot(page)
    if action == "click":
        return await do_click(page, args.get("ref"), ref_map)
    if action == "type":
        return await do_type(page, args.get("ref"), args.get("text"), args.get("key"), ref_map)
    if action == "press":
        return await do_press(page, args.get("key"))
    if action == "scroll":
        return await do_scroll(page, args.get("direction"))
    if action == "back":
        return await do_back(page)
    raise BrowserActionError(f"unsupported action {action}")


async def _begin_handoff(ctx: dict, tkey: str, state: dict, message: str) -> None:
    state.update(
        handoff_active=True,
        handoff_message=message,
        handoff_message_id=ctx.get("message_id"),
        handoff_started_at=time.time(),
    )
    await save_thread_state(tkey, state)
    await mark_handoff(tkey, True)


async def _emit_handoff(ctx: dict, live_url: str | None, message: str) -> None:
    streamer = ctx.get("streamer")
    if not live_url or streamer is None or not hasattr(streamer, "emit_browser_handoff"):
        return
    try:
        await streamer.emit_browser_handoff(
            live_url=live_url, message=message, call_id=ctx.get("tool_call_id"), session_id=None
        )
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: could not emit browser_handoff event: {exc}")


async def _handoff(args: dict, ctx: dict, tkey: str, live_url: str | None, state: dict) -> dict:
    if not live_url:
        return _err("live view is not available for this browser tab")
    message = (args.get("message") or "").strip() or "Please complete the required action in the browser."
    await _begin_handoff(ctx, tkey, state, message)
    await _emit_handoff(ctx, live_url, message)
    return _ok(
        {"handoff": True, "live_url": live_url, "message": message, "instructions": HANDOFF_INSTRUCTIONS},
        action="request_user_action",
    )
