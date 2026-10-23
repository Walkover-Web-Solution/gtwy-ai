"""Entry point for the built-in Gtwy_Browser tool.

``call_gtwy_browser(args, ctx)`` follows the same contract as every other gtwy tool
coroutine: it always returns ``{"response", "metadata", "status": 0|1}`` and never
raises. Each conversation drives its own hosted browser, on the browser service its agent
picked (``providers.py``), with its own logins (``sessions.py``), so two conversations can be
signed in to the same site as different people and neither can see the other's pages.
"""

import asyncio

from globals import logger

from . import providers
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
    looks_like_bot_check,
    looks_like_login_page,
    snapshot_page,
)
from .connection import BrowserConnectionError, is_connection_lost_error, is_timeout_error, target_id_of
from .live_link import signed_live_url
from .provider_base import BrowserServiceError, NoCredits, scrub
from .schema import BROWSER_ACTIONS
from .security import UrlBlocked, ensure_url_allowed, wrap_untrusted
from .session_store import BrowserBusy, get_thread_state, save_thread_state, thread_key
from .sessions import (
    drop_connection,
    mark_handoff,
    open_or_reuse_tab,
    release_tab,
    set_conversation_page,
)

TOOL_TIMEOUT_SECONDS = 60
# Getting the browser ready (locks, profile, starting a browser, CDP connect) is capped
# separately, so a slow start can never eat the time the page itself needs. A new browser takes
# about 10 s, a running one about 1 s. Setup plus a 30 s navigate fits inside the ceiling above.
SETUP_TIMEOUT_SECONDS = 25
# Actions that can make a site open a new tab (a target=_blank link, window.open on a click or a
# key press). The agent is moved to that tab, since it can only see one page at a time.
TAB_OPENING_ACTIONS = {"click", "type", "press"}
NEW_TAB_LOAD_TIMEOUT_MS = 10_000
NEW_TAB_SETTLE_TIMEOUT_MS = 3_000


def _handoff_instructions(live_url: str | None) -> str:
    """What the model must reply after a handoff. The link is spelled out because models given
    both the page's own address (``url``) and ``live_url`` sometimes send the site's sign-in URL,
    and a login in the user's own browser never reaches this one."""
    return (
        f"Reply to the user now with this exact link to the live browser: {live_url} . Tell them "
        "to open it, complete the action there (log in, solve the CAPTCHA, enter the OTP), and "
        "reply 'done' when finished. Do not give them the website's own address or sign-in URL: "
        "a login in their own browser does not reach this browser. Do not take any other browser "
        "action in this turn. When the user replies, call snapshot and continue the original task."
    )


def _reason(exc: Exception) -> str:
    """A short, readable cause. Playwright calls every error "Error", so use its message. Any
    credential in it (a CDP URL with an account key) is masked before the model sees it."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return scrub(text)[:200]


def _err(message: str, **extra) -> dict:
    return {"response": {"error": message, **extra}, "metadata": {"type": "function"}, "status": 0}


def _ok(response: dict, **meta) -> dict:
    return {"response": response, "metadata": {"type": "function", "browser": meta}, "status": 1}


async def call_gtwy_browser(args: dict | None, ctx: dict | None) -> dict:
    try:
        return await asyncio.wait_for(_run(args or {}, ctx or {}), timeout=TOOL_TIMEOUT_SECONDS)
    except TimeoutError:
        return _err(f"the page did not finish within {TOOL_TIMEOUT_SECONDS}s; call snapshot to see where it got to")
    except Exception as exc:  # the tool loop expects a dict, never an exception
        logger.error(f"Gtwy_Browser: unexpected failure: {exc.__class__.__name__}: {exc}")
        return _err(f"browser tool failed: {_reason(exc)}")


async def _run(args: dict, ctx: dict) -> dict:
    try:
        provider, client = providers.resolve(ctx.get("browser_settings"))
    except providers.ProviderUnavailable as exc:
        return _err(str(exc))

    action = (args.get("action") or "").strip()
    if action not in BROWSER_ACTIONS:
        return _err(f"unknown action '{action}'. Valid actions: {', '.join(BROWSER_ACTIONS)}")

    tkey = thread_key(ctx.get("org_id"), ctx.get("thread_id"), ctx.get("sub_thread_id"))

    if action == "navigate":
        # Check the URL before starting a browser so a blocked URL never costs one.
        try:
            await ensure_url_allowed(args.get("url"))
        except UrlBlocked as exc:
            return _err(f"url not allowed: {exc}")

    try:
        page, record = await asyncio.wait_for(
            open_or_reuse_tab(tkey, ctx.get("org_id"), ctx.get("bridge_id"), ctx.get("browser_settings"), provider),
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(f"Gtwy_Browser: getting the browser took over {SETUP_TIMEOUT_SECONDS}s for thread {tkey}")
        return _err(f"the browser took more than {SETUP_TIMEOUT_SECONDS}s to start. Try the same action again")
    except BrowserBusy as busy:
        return _err(str(busy), retry_in_seconds=busy.retry_in)
    except NoCredits as exc:
        logger.error(f"Gtwy_Browser: {client.LABEL} has no credits: {exc}")
        return _err(
            f"the {client.LABEL} browser service has run out of credits. Tell the user to top it up "
            "or pick another browser service for this agent",
            detail=scrub(exc)[:300],
        )
    except BrowserServiceError as exc:
        logger.error(f"Gtwy_Browser: {client.LABEL} error while opening a browser: {exc}")
        return _err(
            f"the {client.LABEL} browser service is unavailable right now; try again later",
            detail=scrub(exc)[:300],
        )
    except BrowserConnectionError as exc:
        logger.warning(f"Gtwy_Browser: connection error while opening the browser: {exc}")
        return _err(
            "the browser did not answer; it may still be starting. Try the same action again in a few seconds",
            detail=scrub(exc)[:300],
        )

    # Prefer gtwy's signed, short-lived link, bound to the user of this conversation; fall back
    # to the browser's own one (a credential for it) only when the gateway has no public address
    # and the browser's service gives a URL that is safe to hand out.
    record_client = providers.get(record.get("provider")) or client
    live_url = signed_live_url(
        ctx.get("org_id"), ctx.get("thread_id"), ctx.get("sub_thread_id"), ctx.get("viewer_id")
    ) or (record.get("live_url") if record_client.RAW_LIVE_URL_SHAREABLE else None)
    state = await get_thread_state(tkey)

    # While the user is acting in the live view we take no snapshots, so nothing they type can
    # land in the transcript. A new user message (new message_id) or a navigate ends the handoff.
    if state.get("handoff_active"):
        user_replied = ctx.get("message_id") and ctx.get("message_id") != state.get("handoff_message_id")
        if action == "navigate" or user_replied:
            state.update(handoff_active=False, handoff_message=None, handoff_message_id=None)
            await save_thread_state(tkey, state)
            await mark_handoff(tkey, False)
        elif action != "request_user_action":
            return _err(
                "user is interacting with the browser; wait for the user to confirm, then call snapshot",
                live_url=live_url,
            )

    if action == "request_user_action":
        return await _handoff(args, ctx, tkey, live_url, state)

    opened_tabs: list = []

    def _on_new_tab(new_page) -> None:
        opened_tabs.append(new_page)

    watched_context = page.context if action in TAB_OPENING_ACTIONS else None
    if watched_context is not None:
        watched_context.on("page", _on_new_tab)
    try:
        response, new_ref_map = await _dispatch(action, args, page, state)
        new_tab = await _opened_tab(opened_tabs)
        if new_tab is not None:
            # The site opened a new tab (Flipkart and Amazon open products this way). Follow it,
            # so the model sees the page it asked for; the earlier tab stays open behind it.
            page = new_tab
            response, new_ref_map = await snapshot_page(page)
            response["opened_new_tab"] = True
            await set_conversation_page(tkey, await target_id_of(page))
            logger.info(f"Gtwy_Browser: thread {tkey} followed a new tab to {page.url[:80]}")
    except StaleRefError as exc:
        return _err(str(exc), live_url=live_url)
    except UrlBlocked as exc:
        return _err(f"url not allowed: {exc}", live_url=live_url)
    except BrowserActionError as exc:
        return _err(str(exc), live_url=live_url)
    except Exception as exc:
        if is_connection_lost_error(exc):
            logger.warning(f"Gtwy_Browser: browser lost for thread {tkey}: {exc.__class__.__name__}")
            await drop_connection(record)
            await release_tab(tkey, reason="browser lost")
            return _err("the browser was lost; call navigate again to start over")
        if is_timeout_error(exc):
            return _err(
                "timeout: the page did not respond in time; call snapshot to see its current state",
                live_url=live_url,
            )
        logger.error(f"Gtwy_Browser: action {action} failed: {exc.__class__.__name__}: {exc}")
        return _err(f"browser action failed: {_reason(exc)}", live_url=live_url)
    finally:
        if watched_context is not None:
            watched_context.remove_listener("page", _on_new_tab)

    if new_ref_map is not None:
        state["ref_map"] = new_ref_map

    # A page asking the user to sign in or to solve a CAPTCHA starts the handoff on its own, so
    # the model never has to remember to call request_user_action and never tries to type
    # credentials or give up on a page it cannot see.
    if new_ref_map is not None:
        if await looks_like_login_page(page):
            await _begin_handoff(ctx, tkey, state, "This page requires you to log in.")
            await _emit_handoff(ctx, live_url, state["handoff_message"])
            response.update(login_required=True, live_url=live_url, instructions=_handoff_instructions(live_url))
        elif looks_like_bot_check(page.url, response.get("snapshot"), len(new_ref_map)):
            await _begin_handoff(ctx, tkey, state, "This site wants to check you are human. Please solve the CAPTCHA.")
            await _emit_handoff(ctx, live_url, state["handoff_message"])
            response.update(captcha_required=True, live_url=live_url, instructions=_handoff_instructions(live_url))

    # The chat UI needs a way to show this conversation's browser at any moment, not only when a
    # login blocks the agent, so every result carries its live link.
    response.setdefault("live_url", live_url)

    await save_thread_state(tkey, state)

    if response.get("snapshot"):
        response["snapshot"] = wrap_untrusted(response["snapshot"])
    if response.get("title"):
        response["title"] = wrap_untrusted(response["title"])

    return _ok(
        response,
        action=action,
        url=page.url,
        provider=record.get("provider") or providers.DEFAULT_PROVIDER,
        browser=(record.get("browser_id") or "")[-6:],
    )


async def _opened_tab(opened_tabs: list):
    """The newest tab the last action opened, loaded enough to read, or None."""
    for new_page in reversed(opened_tabs):
        if new_page.is_closed():
            continue
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=NEW_TAB_LOAD_TIMEOUT_MS)
        except Exception:
            pass
        try:
            await new_page.wait_for_load_state("networkidle", timeout=NEW_TAB_SETTLE_TIMEOUT_MS)
        except Exception:
            pass
        if not new_page.is_closed():
            return new_page
    return None


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
        return _err("live view is not available for this browser")
    message = (args.get("message") or "").strip() or "Please complete the required action in the browser."
    await _begin_handoff(ctx, tkey, state, message)
    await _emit_handoff(ctx, live_url, message)
    return _ok(
        {"handoff": True, "live_url": live_url, "message": message, "instructions": _handoff_instructions(live_url)},
        action="request_user_action",
    )
