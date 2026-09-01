"""Per-user credit caps for embeds.

An embed has one org wallet, but many end users creating agents inside it.
The org's admin can give each user a spending limit (stored by Node in the
`user_credit_limits` Mongo collection). A user past their limit is blocked
even while the org wallet still has credit — a cap on one shared wallet, not
a sub-wallet: no money moves.

This is the fourth scope of the existing limit machine (bridge / folder /
apikey live in src/services/utils/update_and_check_cost.py): limit metadata
from Mongo (Redis-cached), a live usage counter in Redis with the same JSON
shape, reset periods via calculate_limit_ttl.
"""

import json
import re

from globals import logger
from models.mongo_connection import db

from src.configs.constant import redis_keys
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.limit_ttl_utils import calculate_limit_ttl

_LIMIT_CACHE_KEY = "nd_user_credit_limit_"
_LIMIT_CACHE_TTL = 300  # Node CRUD busts this key on every limit change

_user_credit_limits = db["user_credit_limits"]


def sanitize_user_id(user_id) -> str:
    """user_id is client-supplied — normalize it before embedding in Redis keys.

    Node uses the same rule when it busts caches and reads live usage; keep the
    two implementations in sync.
    """
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(user_id))[:64]


def _usage_key(folder_id: str, user_id) -> str:
    return f"{redis_keys['userusedcost_']}{folder_id}_{sanitize_user_id(user_id)}"


def _limit_cache_key(folder_id: str, user_id) -> str:
    return f"{_LIMIT_CACHE_KEY}{folder_id}_{sanitize_user_id(user_id)}"


async def get_user_credit_limit(org_id: str, folder_id: str, user_id) -> dict | None:
    """Fetch the admin-set limit for one embed user. None = no limit set.

    Redis-cached (absence too, as "null", so unlimited users don't re-hit
    Mongo on every request).
    """
    cache_key = _limit_cache_key(folder_id, user_id)
    try:
        cached = await find_in_cache(cache_key)
        if cached is not None:
            parsed = json.loads(cached)
            return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.error(f"[billing] user limit cache read failed ({folder_id}/{user_id}): {e}")

    doc = None
    try:
        doc = await _user_credit_limits.find_one(
            {"org_id": str(org_id), "folder_id": str(folder_id), "user_id": str(user_id)},
            {"_id": 0, "user_limit": 1, "user_limit_reset_period": 1, "user_limit_start_date": 1, "user_usage": 1},
        )
        await store_in_cache(cache_key, doc, ttl=_LIMIT_CACHE_TTL)
    except Exception as e:
        logger.error(f"[billing] user limit lookup failed ({folder_id}/{user_id}): {e}")
    return doc


def _limit_error(current_usage: float, limit_value: float) -> dict:
    # Written for the END USER of the embed: they cannot top anything up —
    # only their workspace admin can raise the allowance.
    return {
        "success": False,
        "error": (
            "You've used your credit limit for this period. "
            "Ask your workspace admin to raise it."
        ),
        "error_code": "USER_LIMIT_EXCEEDED",
        "limit_type": "user",
        "current_usage": current_usage,
        "limit_value": limit_value,
    }


async def check_user_credit_limit(org_id: str, folder_id: str, user_id) -> dict | None:
    """Block when the embed user's spend has reached their admin-set limit.

    Same check-then-count semantics as the folder/bridge/apikey limits: a user
    can overshoot by at most their concurrent in-flight requests.
    """
    if not (org_id and folder_id and user_id):
        return None
    try:
        limit_doc = await get_user_credit_limit(org_id, folder_id, user_id)
        if not limit_doc:
            return None
        limit_value = float(limit_doc.get("user_limit") or 0)
        if limit_value <= 0:
            return None

        usage_value = 0.0
        usage_key = _usage_key(folder_id, user_id)
        cached_usage = await find_in_cache(usage_key)
        if cached_usage is not None:
            try:
                usage_value = float(json.loads(cached_usage).get("usage_value", 0))
            except (ValueError, TypeError, AttributeError):
                usage_value = 0.0
        else:
            # Seed the live counter from the Mongo doc, TTL anchored to the
            # admin-chosen reset period — identical to the other scopes.
            usage_value = float(limit_doc.get("user_usage") or 0)
            reset_period = limit_doc.get("user_limit_reset_period") or "monthly"
            setup_date = limit_doc.get("user_limit_start_date")
            await store_in_cache(
                usage_key,
                {"usage_value": usage_value, "reset_period": reset_period, "setup_date": setup_date},
                ttl=calculate_limit_ttl(reset_period, setup_date),
            )

        if usage_value >= limit_value:
            return _limit_error(usage_value, limit_value)
    except Exception as e:
        # Limit enforcement must never take the request down.
        logger.error(f"[billing] user limit check failed ({folder_id}/{user_id}): {e}")
    return None


async def record_user_usage(parsed_data, expected_cost) -> dict | None:
    """Count a wallet-billed frame's cost against the attributed embed user.

    Called from update_cost next to the bridge/folder/apikey increments. Runs
    for every embed attribution (even without a limit set) so the admin's
    usage view is complete.
    """
    attribution = parsed_data.get("billing_attribution") or {}
    if not (
        attribution.get("is_embed")
        and attribution.get("user_id")
        and attribution.get("folder_id")
        and parsed_data.get("wallet")
        and expected_cost
    ):
        return None
    try:
        from src.services.utils.update_and_check_cost import update_usage_cost_in_cache

        limit_doc = await get_user_credit_limit(
            parsed_data.get("org_id"), attribution["folder_id"], attribution["user_id"]
        ) or {}
        limit_meta = {
            "user": {
                "limit_reset_period": limit_doc.get("user_limit_reset_period"),
                "limit_start_date": limit_doc.get("user_limit_start_date"),
            }
        }
        return await update_usage_cost_in_cache(
            _usage_key(attribution["folder_id"], attribution["user_id"]),
            expected_cost,
            "user",
            limit_meta,
        )
    except Exception as e:
        logger.error(f"[billing] user usage update failed: {e}")
        return None
