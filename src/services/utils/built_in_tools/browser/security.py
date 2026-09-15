"""URL guard and untrusted-content wrapping for the browser tool."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from . import steel_client

BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "169.254.169.254"}
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa")
DNS_TIMEOUT_SECONDS = 5
DNS_ATTEMPTS = 2

UNTRUSTED_START = "<<<UNTRUSTED_WEB_CONTENT>>>"
UNTRUSTED_END = "<<<END_UNTRUSTED_WEB_CONTENT>>>"


class UrlBlocked(Exception):
    """The model asked to navigate somewhere the tool refuses to go."""


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


async def ensure_url_allowed(url: str) -> None:
    """Raise UrlBlocked for non-http(s) schemes, internal hostnames and private/loopback addresses."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise UrlBlocked("url must start with http:// or https://")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UrlBlocked("url has no hostname")
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_SUFFIXES):
        raise UrlBlocked("url points to an internal host, which is not allowed")
    steel = steel_client.steel_host()
    if steel and hostname == steel.lower():
        raise UrlBlocked("url points to the browser backend, which is not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_blocked(literal):
            raise UrlBlocked("url points to a private or reserved address, which is not allowed")
        return

    addresses = None
    last_error = None
    for attempt in range(DNS_ATTEMPTS):
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(_resolve, hostname), timeout=DNS_TIMEOUT_SECONDS)
            break
        except (socket.gaierror, TimeoutError, OSError) as exc:
            # Transient resolver hiccups are common; retry once before failing closed.
            last_error = exc
            if attempt + 1 < DNS_ATTEMPTS:
                await asyncio.sleep(0.5)
    if addresses is None:
        raise UrlBlocked(f"could not resolve host {hostname}") from last_error
    for address in addresses:
        try:
            if _ip_blocked(ipaddress.ip_address(address)):
                raise UrlBlocked("url resolves to a private or reserved address, which is not allowed")
        except ValueError:
            continue


def wrap_untrusted(text: str | None) -> str:
    return f"{UNTRUSTED_START}\n{text or ''}\n{UNTRUSTED_END}"
