"""Shared validation primitives for request schemas."""

import re

# Accepts http/https, FQDN, bare hostnames (localhost, Docker service names),
# IPv4, optional port, and path/query/fragment. Rejects other schemes.
_HTTP_URL_PATTERN = re.compile(
    r"^https?://"
    r"(?:(?:\d{1,3}\.){3}\d{1,3}|[^\s/?#:]+)"  # IPv4 or hostname
    r"(?::\d{1,5})?"                              # optional port
    r"(?:[/?#][^\s]*)?$",                         # optional path/query/fragment
    re.IGNORECASE,
)

HTTP_URL_REGEX = _HTTP_URL_PATTERN.pattern



def is_valid_http_url(value: str) -> bool:
    """Return True if *value* is a valid http:// or https:// URL."""
    return bool(_HTTP_URL_PATTERN.fullmatch(value))
