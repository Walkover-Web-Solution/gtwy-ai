"""What every hosted browser service client shares: errors, the REST call, proxy country,
profile names and keeping account keys out of URLs, logs and model-facing errors.

Each service has one client module built the same way (``browser_use_client.py``,
``browserbase_client.py``, ``steel_cloud_client.py``, ``kernel_client.py``,
``anchor_client.py``), and every client returns the same normalized browser view::

    {"id", "cdp_url", "live_url", "timeout_at", "profile_id", "active", "started_at"}

``cdp_url`` never holds an account API key: a service whose CDP URL needs one gets it added
by the client's ``connect_url()`` at connect time, so Redis only ever stores session-scoped URLs.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

HTTP_TIMEOUT = 20.0
COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}$")
# Every browser goes through a residential proxy in this country unless the agent's browser
# settings pick another one, or turn it off with proxy_country "none". The proxy gets past
# much more bot protection (Myntra only loads through it) but its traffic is billed per GB.
DEFAULT_PROXY_COUNTRY = "in"
PROXY_OFF_VALUES = {"none", "off", "false", "no"}
# Query parameters that carry a credential in a service URL.
SECRET_PARAMS = ("apiKey", "api_key", "apikey", "signingKey", "token", "jwt", "key")
_SECRET_RE = re.compile(r"(?i)\b(" + "|".join(SECRET_PARAMS) + r")=([^&\s\"'<>]+)")


class BrowserServiceError(Exception):
    """A browser service REST call failed or the service is unreachable."""

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(scrub(message))
        self.status = status
        self.detail = scrub(detail)


class TooManyBrowsers(BrowserServiceError):
    """The service account is at its limit of browsers running (or starting) at once."""


class NoCredits(BrowserServiceError):
    """The service account has run out of credits."""


class ProfileNotFound(BrowserServiceError):
    """The saved-login profile given to a new browser does not exist on the service."""


class ProfileBusy(BrowserServiceError):
    """The saved-login profile is still being saved by the browser that just stopped."""


def scrub(text) -> str:
    """The text with every credential query value replaced, safe for logs and the model."""
    return _SECRET_RE.sub(r"\1=***", str(text or ""))


def redact(value: str | None) -> str:
    """Browser and profile ids in logs: enough to match lines, not a full id."""
    if not value:
        return "<none>"
    return f"...{str(value)[-6:]}"


def proxy_country(value) -> str | None:
    """The proxy country (lowercase ISO code) for an agent's ``proxy_country`` setting, or None
    for no proxy. Not set (or not a two-letter code) means DEFAULT_PROXY_COUNTRY."""
    country = str(value if value is not None else "").strip().lower()
    if country in PROXY_OFF_VALUES or value is False:
        return None
    return country if COUNTRY_CODE_RE.match(country) else DEFAULT_PROXY_COUNTRY


def profile_name(tkey: str) -> str:
    """A stable, service-safe profile name for a conversation (its key has colons)."""
    return f"gtwy-{hashlib.sha1(tkey.encode()).hexdigest()[:20]}"


def without_param(url: str | None, name: str) -> str | None:
    """The URL without one query parameter (used to keep an account key out of Redis)."""
    if not url:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    return urlunsplit(parts._replace(query=urlencode(query)))


def with_param(url: str, name: str, value: str) -> str:
    """The URL with one query parameter set."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(body.get("detail") or body.get("message") or error or body)[:300]
    return str(body)[:300]


async def request(
    service: str,
    method: str,
    url: str,
    headers: dict,
    json_body: dict | None = None,
    params=None,
    with_headers: bool = False,
):
    """One REST call to a browser service. Returns the parsed JSON body (or None), plus the
    response headers when ``with_headers``. Raises BrowserServiceError (429 and 529 as
    TooManyBrowsers, 402 as NoCredits) with the status and the service's own error text."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.request(method, url, json=json_body, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise BrowserServiceError(f"{service} unreachable: {exc.__class__.__name__}: {str(exc)[:120]}") from exc
    if response.status_code >= 400:
        detail = _detail(response)
        path = urlsplit(url).path
        message = f"{service} {method} {path} returned {response.status_code}: {detail}"
        if response.status_code in (429, 529):
            raise TooManyBrowsers(message, response.status_code, detail)
        if response.status_code == 402:
            raise NoCredits(message, response.status_code, detail)
        raise BrowserServiceError(message, response.status_code, detail)
    data = None
    if response.content:
        try:
            data = response.json()
        except ValueError:
            data = None
    return (data, response.headers) if with_headers else data


def mentions_profile(exc: BrowserServiceError) -> bool:
    text = f"{exc.detail} {exc}".lower()
    return any(word in text for word in ("profile", "context"))
