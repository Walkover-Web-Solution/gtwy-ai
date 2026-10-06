"""Durable copy of API-key spend, on the apikey document itself.

Redis holds the counter that enforcement reads on every request. These two fields
on ``apikeycredentials`` are the copy that survives it:

    apikey_usage:        47.20
    apikey_usage_period: "2026-08"

The period is what makes the pair safe to read. A value left over from a finished
window is not mistaken for current spend -- the stored period simply will not
match the one being asked about, and the answer is zero. Nothing resets these
fields; a new window overwrites them on its first write.

Only the current window is kept, by design: one document can hold one period, so
there is no history here.
"""

import logging

from bson import ObjectId, errors

from models.mongo_connection import db

logger = logging.getLogger(__name__)

apikeyCredentialsModel = db["apikeycredentials"]


def _object_id(apikey_id):
    """Coerce to ObjectId, or None when the value cannot be one."""
    if isinstance(apikey_id, ObjectId):
        return apikey_id
    try:
        return ObjectId(str(apikey_id))
    except (errors.InvalidId, TypeError, ValueError):
        logger.error(f"Not a valid apikey id: {apikey_id!r}")
        return None


async def add_usage(apikey_id, period, cost) -> bool:
    """Add ``cost`` to this key's spend for ``period``.

    Uses an aggregation-pipeline update so the reset and the increment are one
    atomic step: if the stored period is not ``period`` the accumulated total is
    discarded and we start from ``cost``, otherwise we add to it. Doing this as a
    read-then-write would reintroduce exactly the lost-increment bug that moving
    the counter to INCRBYFLOAT was meant to fix.
    """
    oid = _object_id(apikey_id)
    if oid is None or not period:
        return False

    try:
        result = await apikeyCredentialsModel.update_one(
            {"_id": oid},
            [
                {
                    "$set": {
                        "apikey_usage": {
                            "$add": [
                                {
                                    "$cond": [
                                        {"$eq": [{"$ifNull": ["$apikey_usage_period", None]}, period]},
                                        {"$ifNull": ["$apikey_usage", 0]},
                                        0,
                                    ]
                                },
                                float(cost),
                            ]
                        },
                        "apikey_usage_period": period,
                    }
                }
            ],
        )
        return result.matched_count > 0
    except Exception as e:
        logger.error(f"Error adding apikey usage for {apikey_id} period {period}: {str(e)}")
        return False


async def get_usage(apikey_id, period) -> float | None:
    """Spend recorded for this key in ``period``, or ``None`` if there is none.

    ``None`` covers both "no document" and "the stored value belongs to a
    finished window" -- the caller treats either as nothing spent yet.

    Read straight from Mongo rather than from the agent config: that payload is
    Redis-cached, so the ``apikey_usage`` it carries is stale as of cache time.
    """
    oid = _object_id(apikey_id)
    if oid is None or not period:
        return None

    try:
        row = await apikeyCredentialsModel.find_one(
            {"_id": oid},
            {"apikey_usage": 1, "apikey_usage_period": 1},
        )
        if not row or row.get("apikey_usage_period") != period:
            return None
        return float(row.get("apikey_usage", 0) or 0)
    except Exception as e:
        logger.error(f"Error reading apikey usage for {apikey_id} period {period}: {str(e)}")
        return None
