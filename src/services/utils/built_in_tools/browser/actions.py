"""One coroutine per browser action. All run against an existing Playwright page.

Every mutating action returns a fresh snapshot so the model does not need a
separate ``snapshot`` call after it. Functions return ``(response, ref_map)``;
``ref_map`` is ``None`` when the action did not produce a new snapshot.
"""

import re
from urllib.parse import urlparse

from .security import ensure_url_allowed
from .snapshot import parse_aria_snapshot, truncate

NAVIGATE_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 3_000
SCROLL_PIXELS = 600
# Cut huge pages so one snapshot cannot eat the model's context.
SNAPSHOT_MAX_CHARS = 15_000
LOGIN_PATH_RE = re.compile(
    r"/(ap/signin|signin|sign-in|sign_in|login|log-in|log_in|auth/login|sessions/new|account/login|"
    r"oauth2?/(auth|authorize)|authorize|sso)(/|$|\?)",
    re.IGNORECASE,
)
LOGIN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "appleid.apple.com")


class BrowserActionError(Exception):
    """User-facing failure of a single action (bad args, refused input, ...)."""


class StaleRefError(BrowserActionError):
    pass


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
    except Exception:
        pass
    await page.wait_for_timeout(300)


async def snapshot_page(page) -> tuple[dict, dict]:
    yaml_text = await page.locator("body").aria_snapshot(timeout=ACTION_TIMEOUT_MS)
    text, ref_map = parse_aria_snapshot(yaml_text)
    text, truncated, ref_map = truncate(text, SNAPSHOT_MAX_CHARS, ref_map)
    response = {
        "url": page.url,
        "title": await page.title(),
        "snapshot": text,
        "truncated": truncated,
        "ref_count": len(ref_map),
    }
    return response, ref_map


def resolve_ref(page, ref: str | None, ref_map: dict):
    if not ref:
        raise BrowserActionError("ref is required for this action; take a snapshot and pick a ref like e5")
    ref = ref.strip().lstrip("@").strip("[]")
    meta = (ref_map or {}).get(ref)
    if not meta:
        raise StaleRefError(f"stale ref {ref}: the page changed since the last snapshot. Call snapshot and use the new refs")
    if meta.get("name"):
        locator = page.get_by_role(meta["role"], name=meta["name"], exact=True)
    else:
        locator = page.get_by_role(meta["role"])
    return locator.nth(int(meta.get("nth") or 0))


async def do_navigate(page, url: str | None):
    if not url:
        raise BrowserActionError("url is required for navigate")
    await ensure_url_allowed(url)
    await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except Exception:
        pass
    return await snapshot_page(page)


async def do_snapshot(page):
    return await snapshot_page(page)


async def do_click(page, ref: str | None, ref_map: dict):
    locator = resolve_ref(page, ref, ref_map)
    await locator.click(timeout=ACTION_TIMEOUT_MS)
    await _settle(page)
    return await snapshot_page(page)


async def do_type(page, ref: str | None, text: str | None, key: str | None, ref_map: dict):
    if text is None:
        raise BrowserActionError("text is required for type")
    locator = resolve_ref(page, ref, ref_map)
    input_type = None
    try:
        input_type = await locator.get_attribute("type", timeout=ACTION_TIMEOUT_MS)
    except Exception:
        pass
    if (input_type or "").lower() == "password":
        raise BrowserActionError(
            "refusing to type into a password field; call request_user_action so the user can log in themselves"
        )
    await locator.fill(str(text), timeout=ACTION_TIMEOUT_MS)
    if key:
        await locator.press(key, timeout=ACTION_TIMEOUT_MS)
    await _settle(page)
    return await snapshot_page(page)


async def do_press(page, key: str | None):
    if not key:
        raise BrowserActionError("key is required for press (e.g. Enter, Escape, Tab)")
    await page.keyboard.press(key)
    await _settle(page)
    return await snapshot_page(page)


async def do_scroll(page, direction: str | None):
    delta = -SCROLL_PIXELS if (direction or "down").lower() == "up" else SCROLL_PIXELS
    await page.mouse.wheel(0, delta)
    await page.wait_for_timeout(300)
    return await snapshot_page(page)


async def do_back(page):
    await page.go_back(wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
    await _settle(page)
    return await snapshot_page(page)


async def looks_like_login_page(page) -> bool:
    """Heuristic: the current page is asking the user to sign in.

    True when the URL path looks like a login route, the host is a known identity
    provider, or a visible password field is present. Used to start the user handoff
    automatically instead of relying on the model to remember to.
    """
    url = page.url or ""
    parsed = urlparse(url)
    if any(host in (parsed.hostname or "") for host in LOGIN_HOSTS):
        return True
    if LOGIN_PATH_RE.search(parsed.path or ""):
        return True
    try:
        return await page.locator("input[type='password']:visible").count() > 0
    except Exception:
        return False
