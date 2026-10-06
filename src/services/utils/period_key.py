"""Period identifiers for usage counters.

A usage counter carries its own period in its key (Redis) or row (Mongo) instead
of being zeroed by a scheduled job or reset by TTL expiry. ``period_key`` turns a
reset period plus an instant into the string identifying the current window:

    daily   -> "2026-08-25"   rolls at midnight
    weekly  -> "2026-W35"     rolls Monday, ISO week numbering
    monthly -> "2026-08"      rolls on the 1st

A window therefore resets because the *string changes* and the new key has never
been written -- nothing has to expire or be set to zero for the budget to be
fresh. That makes the reset exact, and removes the failure mode where a missed
scheduled job leaves every capped key stuck at the previous period's total.

Expiry becomes housekeeping rather than correctness: a stale key is simply never
read again, so a late sweep costs nothing. And because the window is in the key,
an absent key means "this window is new", never "the data was lost" -- so there is
no second store to fall back to. Redis is configured for durability and is the
only place this lives.

Weekly uses ISO week numbering (``%G-W%V``, not ``%Y-W%U``) so the days either
side of New Year land in one week rather than being split across two different
strings.

All formatting is in UTC -- see ``PERIOD_TIMEZONE``. The Node service builds the
same strings in ``src/services/utils/periodKey.utils.js``; the two must agree
exactly, so both repos assert against the same vectors (see the test files).
"""

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# The timezone that defines "the 1st" and "Monday". UTC keeps the two services
# trivially consistent; changing it means changing it in the Node helper too.
PERIOD_TIMEZONE = UTC

# Matches the Mongo schema default and the ``or "monthly"`` fallbacks elsewhere.
DEFAULT_RESET_PERIOD = "monthly"

_FORMATS = {
    "daily": "%Y-%m-%d",
    "weekly": "%G-W%V",
    "monthly": "%Y-%m",
}

# How long a Redis counter lives. Only needs to comfortably outlast its own
# period -- nothing reads a past period, so precision buys nothing here.
REDIS_TTL_SECONDS = {
    "daily": 2 * 86400,
    "weekly": 15 * 86400,
    "monthly": 65 * 86400,
}

def normalize_reset_period(reset_period: str | None) -> str:
    """Coerce a stored reset period to one we know how to format."""
    candidate = (reset_period or "").lower().strip()
    if candidate in _FORMATS:
        return candidate
    if candidate:
        logger.warning(f"Unknown reset_period {candidate!r}; treating as {DEFAULT_RESET_PERIOD}")
    return DEFAULT_RESET_PERIOD


def period_key(reset_period: str | None, now: datetime | None = None) -> str:
    """Return the period string for ``now`` under ``reset_period``."""
    reset_period = normalize_reset_period(reset_period)
    moment = now or datetime.now(PERIOD_TIMEZONE)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=PERIOD_TIMEZONE)
    return moment.astimezone(PERIOD_TIMEZONE).strftime(_FORMATS[reset_period])


def redis_ttl_for(reset_period: str | None) -> int:
    """Seconds a Redis counter for this reset period should live."""
    return REDIS_TTL_SECONDS[normalize_reset_period(reset_period)]
