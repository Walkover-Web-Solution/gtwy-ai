import calendar
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MIN_TTL = 60
DEFAULT_TTL = 86400


def calculate_limit_ttl(reset_period: str, setup_date: datetime | None = None) -> int:
    try:
        now = datetime.now(timezone.utc)
        reset_period = (reset_period or "monthly").lower().strip()

        if isinstance(setup_date, str):
            setup_date = datetime.fromisoformat(setup_date)

        anchor = setup_date

        reset_calculators = {
            "monthly": _calculate_next_monthly_reset,
            "weekly": _calculate_next_weekly_reset,
            "daily": _calculate_next_daily_reset,
        }

        calculator = reset_calculators.get(reset_period)
        if not calculator:
            logger.warning(f"Unknown reset_period: {reset_period}, using daily")
            calculator = _calculate_next_daily_reset

        next_reset = calculator(now, anchor)
        ttl_seconds = int((next_reset - now).total_seconds())

        return max(ttl_seconds, MIN_TTL)

    except Exception as e:
        logger.error(f"Error calculating limit TTL: {str(e)}")
        return DEFAULT_TTL


def _calculate_next_daily_reset(now: datetime, anchor: datetime | None = None) -> datetime:
    """
    Next reset is at the same time-of-day as anchor (setup_date).
    If anchor is None, defaults to midnight.

    Example:
        anchor = Jan 2, 04:30
        now    = Jan 4, 03:30  → next reset = Jan 4, 04:30  (TTL = 1h)
        now    = Jan 4, 05:00  → next reset = Jan 5, 04:30  (TTL = ~23.5h)
    """
    if anchor is None:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    reset_time_today = now.replace(hour=anchor.hour, minute=anchor.minute, second=anchor.second, microsecond=0)
    if reset_time_today > now:
        return reset_time_today
    return reset_time_today + timedelta(days=1)


def _calculate_next_weekly_reset(now: datetime, anchor: datetime | None = None) -> datetime:
    """
    Next reset is on the same weekday and time-of-day as anchor (setup_date).
    If anchor is None, defaults to next Monday at midnight.

    Example:
        anchor = Wednesday, 04:30
        now    = Friday 03:00  → next reset = next Wednesday 04:30
        now    = Wednesday 05:00 → next reset = next Wednesday 04:30 (+7 days)
    """
    if anchor is None:
        current_weekday = now.weekday()
        days_until_monday = (7 - current_weekday) % 7 or 7
        return (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)

    anchor_weekday = anchor.weekday()
    reset_time_this_week = now.replace(hour=anchor.hour, minute=anchor.minute, second=anchor.second, microsecond=0)
    current_weekday = now.weekday()
    days_diff = (anchor_weekday - current_weekday) % 7
    candidate = reset_time_this_week + timedelta(days=days_diff)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _calculate_next_monthly_reset(now: datetime, anchor: datetime | None = None) -> datetime:
    """
    Next reset is on the same day-of-month and time-of-day as anchor (setup_date).
    If anchor is None, defaults to the 1st of next month at midnight.
    Clamps day to last valid day of month to handle months shorter than anchor day.

    Example:
        anchor = Jan 2, 04:30
        now    = Feb 1, 03:00  → next reset = Feb 2, 04:30
        now    = Feb 2, 05:00  → next reset = Mar 2, 04:30
        anchor = Jan 31, 04:30
        now    = Feb 28, 03:00 → next reset = Feb 28, 04:30 (clamped)
    """
    if anchor is None:
        if now.month == 12:
            return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def clamp_day(year, month, day):
        max_day = calendar.monthrange(year, month)[1]
        return min(day, max_day)

    anchor_day = anchor.day
    clamped_day = clamp_day(now.year, now.month, anchor_day)
    candidate = now.replace(day=clamped_day, hour=anchor.hour, minute=anchor.minute, second=anchor.second, microsecond=0)

    if candidate <= now:
        next_month = now.month + 1 if now.month < 12 else 1
        next_year = now.year if now.month < 12 else now.year + 1
        clamped_day = clamp_day(next_year, next_month, anchor_day)
        candidate = now.replace(year=next_year, month=next_month, day=clamped_day, hour=anchor.hour, minute=anchor.minute, second=anchor.second, microsecond=0)

    return candidate
