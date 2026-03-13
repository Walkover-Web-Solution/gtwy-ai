from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(tags=["time"])


@router.get(
    "/utc",
    summary="Get current UTC time",
    description=(
        "Returns the current date-time in UTC as an ISO 8601 string with a 'Z' suffix, "
        "along with the Unix epoch timestamp in seconds."
    ),
)
async def get_utc_time():
    now_utc = datetime.now(timezone.utc)

    # ISO 8601 with a Z suffix (e.g. 2026-03-13T06:02:18Z)
    utc_datetime = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "utc_datetime": utc_datetime,
        "unix_epoch_seconds": int(now_utc.timestamp()),
    }
