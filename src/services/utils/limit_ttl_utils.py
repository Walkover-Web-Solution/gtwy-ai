import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MIN_TTL = 60
DEFAULT_TTL = 86400


def calculate_limit_ttl(reset_period: str, setup_date: datetime | str | None = None) -> int:
    try:
        now = datetime.now(IST)
        reset_period = (reset_period or "monthly").lower().strip()
        
        reset_calculators = {
            "monthly": _calculate_next_monthly_reset,
            "weekly": _calculate_next_weekly_reset,
            "daily": _calculate_next_daily_reset,
        }
        
        calculator = reset_calculators.get(reset_period)
        if not calculator:
            logger.warning(f"Unknown reset_period: {reset_period}, using daily")
            calculator = _calculate_next_daily_reset
        
        next_reset = calculator(now)
        ttl_seconds = int((next_reset - now).total_seconds())
        
        return max(ttl_seconds, MIN_TTL)
        
    except Exception as e:
        logger.error(f"Error calculating limit TTL: {str(e)}")
        return DEFAULT_TTL


def _calculate_next_monthly_reset(now: datetime) -> datetime:
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _calculate_next_weekly_reset(now: datetime) -> datetime:
    current_weekday = now.weekday()
    days_until_monday = (7 - current_weekday) % 7 or 7
    return (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)


def _calculate_next_daily_reset(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
