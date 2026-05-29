"""Usage-limit email alerts.

Sits on top of the existing cost-limit system. After each successful request,
``update_cost`` calls :func:`evaluate_usage_alerts` (already on a background
task, so there is no added request latency). For every scope that has a limit
configured (bridge / folder / apikey) it checks three situations and sends a
single email for each via the mail API:

1. Threshold reached  -- cumulative usage crossed ``threshold_percent`` of the limit.
2. Limit reached      -- cumulative usage hit 100% of the limit.
3. Daily spike        -- today's spend exceeded ``spike_multiplier`` x the trailing
                         daily average (needs a few days of history first).

De-duplication: each email is claimed once via an atomic Redis marker. Threshold
and limit markers carry the limit's reset TTL, so they re-arm automatically each
new period. Spike markers live for the current day only.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from config import Config
from src.configs.constant import (
    USAGE_ALERT_MAIL_URL,
    redis_keys,
    usage_alert_config,
    usage_mail_types,
)

from ..cache_service import acquire_lock, find_in_cache, store_in_cache
from .apiservice import fetch
from .limit_ttl_utils import calculate_limit_ttl

logger = logging.getLogger(__name__)

# Scopes monitored and the source field for each scope's identifier in parsed_data.
_MONITORED_SCOPES = ("bridge", "folder", "apikey")


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _date_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _daily_key(scope: str, identifier: str, date_str: str) -> str:
    return f"{redis_keys['dailyusedcost_']}{scope}_{identifier}_{date_str}"


async def _read_bucket(key: str) -> float:
    """Read a per-day spend bucket as a float (0.0 when missing)."""
    cached = await find_in_cache(key)
    if cached is None:
        return 0.0
    try:
        return float(json.loads(cached))
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            return float(cached)
        except (TypeError, ValueError):
            return 0.0


async def _bump_daily_bucket(scope: str, identifier: str, cost: float) -> float:
    """Add ``cost`` to today's per-day bucket and return the new daily total."""
    key = _daily_key(scope, identifier, _date_str(_today()))
    new_total = await _read_bucket(key) + cost
    ttl = int(usage_alert_config["daily_bucket_ttl_days"] * 86400)
    await store_in_cache(key, new_total, ttl=ttl)
    return new_total


async def _current_usage(scope: str, identifier: str, redis_usage: dict) -> float:
    """Authoritative cumulative usage for the scope (from redis_usage, else cache)."""
    data = redis_usage.get(scope)
    if isinstance(data, dict) and data.get("usage_value") is not None:
        try:
            return float(data["usage_value"])
        except (TypeError, ValueError):
            pass
    cached = await find_in_cache(f"{redis_keys[f'{scope}usedcost_']}{identifier}")
    if cached:
        try:
            return float(json.loads(cached).get("usage_value", 0) or 0)
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return 0.0
    return 0.0


async def _claim_alert(marker_key: str, ttl: int) -> bool:
    """Atomically claim the right to send one alert.

    Reuses ``acquire_lock`` (Redis SET NX EX): returns True only for the first
    caller within the TTL window, giving once-per-period de-duplication.
    """
    try:
        return await acquire_lock(marker_key, ttl=int(max(ttl, 60)))
    except Exception:
        return False


async def send_usage_alert_email(mail_type: str, scope: str, parsed_data: dict, details: dict) -> None:
    """POST the alert to the mail API with the mail-type enum and agent details.

    Endpoint, enum values and body shape come from the mail API spec
    (``USAGE_ALERT_MAIL_URL`` and ``usage_mail_types`` in constants). Failures are
    swallowed -- an email problem must never break the request's cost update.
    """
    try:
        payload = {
            "mail_type": mail_type,
            "scope": scope,
            "agent_id": parsed_data.get("bridge_id"),
            "org_id": parsed_data.get("org_id"),
            "org_name": parsed_data.get("org_name"),
            "user_id": parsed_data.get("user_id"),
            "agent_name": parsed_data.get("name"),
            "service": parsed_data.get("service"),
            "folder_id": parsed_data.get("folder_id"),
            **details,
        }
        if getattr(Config, "ENVIRONMENT", None):
            payload["environment"] = Config.ENVIRONMENT
        response, _ = await fetch(USAGE_ALERT_MAIL_URL, method="POST", headers={}, json_body=payload)
        logger.info(
            f"[usage-alert] EMAIL SENT type={mail_type} scope={scope} "
            f"id={parsed_data.get('bridge_id')} response={response}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to send usage alert email (type={mail_type}, scope={scope}): {e}")
        return False


async def _check_threshold_and_limit(
    scope: str,
    identifier: str,
    parsed_data: dict,
    usage_value: float,
    limit_value: float,
    reset_period: str,
    setup_date,
) -> None:
    """Send a threshold (e.g. 80%) email, then a limit-reached (100%) email."""
    percent = (usage_value / limit_value) if limit_value > 0 else 0.0
    # Markers re-arm each new period: TTL = time until the limit resets.
    ttl = calculate_limit_ttl(reset_period, setup_date)
    details = {
        "current_usage": round(usage_value, 6),
        "limit_value": limit_value,
        "percent_used": round(percent * 100, 2),
        "reset_period": reset_period,
    }

    if usage_value >= limit_value:
        marker = f"{redis_keys['usagealertsent_']}{scope}_{identifier}_limit"
        if await _claim_alert(marker, ttl):
            await send_usage_alert_email(usage_mail_types["limit_reached"], scope, parsed_data, details)
    elif percent >= usage_alert_config["threshold_percent"]:
        marker = f"{redis_keys['usagealertsent_']}{scope}_{identifier}_threshold"
        if await _claim_alert(marker, ttl):
            await send_usage_alert_email(usage_mail_types["threshold"], scope, parsed_data, details)


async def _check_spike(
    scope: str,
    identifier: str,
    parsed_data: dict,
    today_total: float,
    usage_value: float,
    limit_value: float,
    reset_period: str,
) -> None:
    """Send a spike email when today's spend dwarfs the trailing daily average."""
    cfg = usage_alert_config
    today = _today()

    # Read the prior N days (excluding today) concurrently; ignore empty days.
    date_strs = [_date_str(today - timedelta(days=i)) for i in range(1, int(cfg["spike_window_days"]) + 1)]
    prior_values = await asyncio.gather(*(_read_bucket(_daily_key(scope, identifier, ds)) for ds in date_strs))
    prior_values = [v for v in prior_values if v > 0]

    # Cold-start guard: need enough history before a busy day looks like a spike.
    if len(prior_values) < cfg["spike_min_history_days"]:
        return

    avg = sum(prior_values) / len(prior_values)
    if avg <= 0 or today_total <= cfg["spike_multiplier"] * avg:
        return

    marker = f"{redis_keys['usagespikealert_']}{scope}_{identifier}_{_date_str(today)}"
    if await _claim_alert(marker, ttl=2 * 86400):
        await send_usage_alert_email(
            usage_mail_types["spike"],
            scope,
            parsed_data,
            {
                "today_spend": round(today_total, 6),
                "avg_daily_spend": round(avg, 6),
                "multiplier": cfg["spike_multiplier"],
                "current_usage": round(usage_value, 6),
                "limit_value": limit_value,
                "reset_period": reset_period,
            },
        )


async def evaluate_usage_alerts(parsed_data: dict, redis_usage: dict | None = None) -> None:
    """Entry point: evaluate threshold / limit / spike alerts for every limited scope.

    Called from ``update_cost`` after the usage counters are incremented. Fully
    guarded so it can never break the background cost-update task.
    """
    try:
        limit = parsed_data.get("limit") or {}
        service = parsed_data.get("service")
        apikey_id = (parsed_data.get("apikey_object_id") or {}).get(service)
        redis_usage = redis_usage or {}
        try:
            expected_cost = float(parsed_data.get("tokens", {}).get("total_cost", 0) or 0)
        except (TypeError, ValueError):
            expected_cost = 0.0

        scope_identifiers = {
            "bridge": parsed_data.get("bridge_id"),
            "folder": parsed_data.get("folder_id"),
            "apikey": apikey_id,
        }

        for scope in _MONITORED_SCOPES:
            identifier = scope_identifiers.get(scope)
            if not identifier:
                continue

            scope_limit = limit.get(scope) or {}
            try:
                limit_value = float(scope_limit.get("limit") or 0)
            except (TypeError, ValueError):
                limit_value = 0.0
            # Only monitor scopes that actually have a limit configured.
            if limit_value <= 0:
                continue

            reset_period = scope_limit.get("limit_reset_period") or "monthly"
            setup_date = scope_limit.get("limit_start_date")
            usage_value = await _current_usage(scope, identifier, redis_usage)

            # TEMP debug: shows why an alert did / didn't fire. Remove once verified.
            logger.info(
                f"[usage-alert] scope={scope} id={identifier} usage={usage_value} "
                f"limit={limit_value} percent={(usage_value / limit_value * 100) if limit_value else 0:.2f}% "
                f"threshold_at={usage_alert_config['threshold_percent'] * 100:.0f}%"
            )

            # Per-day bucket feeds spike detection. Bump when this request had a
            # cost, otherwise just read today's running total.
            if expected_cost > 0:
                today_total = await _bump_daily_bucket(scope, identifier, expected_cost)
            else:
                today_total = await _read_bucket(_daily_key(scope, identifier, _date_str(_today())))

            await _check_spike(scope, identifier, parsed_data, today_total, usage_value, limit_value, reset_period)
            await _check_threshold_and_limit(
                scope, identifier, parsed_data, usage_value, limit_value, reset_period, setup_date
            )

    except Exception as e:
        logger.error(f"Error evaluating usage alerts: {str(e)}")
