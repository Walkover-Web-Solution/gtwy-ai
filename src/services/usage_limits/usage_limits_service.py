import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from redis.asyncio import Redis

from config import Config
from src.schemas.usage_limits import UsageCheckRequest, UsageCheckResponse, UsageLimitConfig
from src.services.usage_limits.redis_lua_scripts import CHECK_AND_RESERVE_SCRIPT, GET_USAGE_SCRIPT, SETTLE_DIFFERENCE_SCRIPT

logger = logging.getLogger(__name__)

redis_client = Redis.from_url(Config.REDIS_URI)
REDIS_PREFIX = f"quota:{Config.ENVIRONMENT}:"


class UsageLimitsService:
    def __init__(self):
        self.redis = redis_client
        self.check_and_reserve_script = None
        self.settle_difference_script = None
        self.get_usage_script = None

    async def initialize(self):
        """Register Lua scripts with Redis."""
        try:
            self.check_and_reserve_script = await self.redis.script_load(CHECK_AND_RESERVE_SCRIPT)
            self.settle_difference_script = await self.redis.script_load(SETTLE_DIFFERENCE_SCRIPT)
            self.get_usage_script = await self.redis.script_load(GET_USAGE_SCRIPT)
            logger.info("Usage limits Lua scripts registered successfully")
        except Exception as e:
            logger.error(f"Failed to register Lua scripts: {e}")
            raise

    def _build_redis_key(self, entity_type: str, entity_id: str, period: str, start_date: datetime) -> str:
        """Build Redis key for usage tracking."""
        if period == "monthly":
            period_key = start_date.strftime("%Y-%m")
        elif period == "weekly":
            period_key = start_date.strftime("%Y-W%W")
        elif period == "daily":
            period_key = start_date.strftime("%Y-%m-%d")
        else:
            period_key = start_date.strftime("%Y-%m")

        return f"{REDIS_PREFIX}{entity_type}:{entity_id}:{period_key}"

    async def check_and_reserve(
        self,
        org_id: str,
        bridge_id: str,
        folder_id: Optional[str],
        apikey_id: Optional[str],
        estimated_cost: float,
        limits: dict,
    ) -> tuple[bool, Optional[dict]]:
        """
        Atomically check if usage is under limits and reserve the estimated cost.
        Returns (allowed, error_details).
        """
        try:
            bridge_limit = limits.get("bridge", {}).get("limit", 0)
            bridge_period = limits.get("bridge", {}).get("reset_period", "monthly")
            bridge_start = limits.get("bridge", {}).get("start_date", datetime.utcnow())

            folder_limit = limits.get("folder", {}).get("limit", 0) if folder_id else 0
            folder_period = limits.get("folder", {}).get("reset_period", "monthly")
            folder_start = limits.get("folder", {}).get("start_date", datetime.utcnow())

            apikey_limit = limits.get("apikey", {}).get("limit", 0) if apikey_id else 0
            apikey_period = limits.get("apikey", {}).get("reset_period", "monthly")
            apikey_start = limits.get("apikey", {}).get("start_date", datetime.utcnow())

            bridge_key = self._build_redis_key("bridge", bridge_id, bridge_period, bridge_start)
            folder_key = self._build_redis_key("folder", folder_id or "none", folder_period, folder_start)
            apikey_key = self._build_redis_key("apikey", apikey_id or "none", apikey_period, apikey_start)

            result = await self.redis.evalsha(
                self.check_and_reserve_script,
                3,
                bridge_key,
                folder_key,
                apikey_key,
                bridge_limit,
                folder_limit,
                apikey_limit,
                estimated_cost,
            )

            if result[0] == 1:
                return True, None

            limit_type = result[1]
            current_usage = result[2]
            limit_value = result[3]

            error = {
                "limit_type": limit_type,
                "current_usage": current_usage,
                "limit_value": limit_value,
                "message": f"{limit_type.capitalize()} limit exceeded. Used: {current_usage}/{limit_value}",
            }
            return False, error

        except Exception as e:
            logger.error(f"Error in check_and_reserve: {e}")
            raise

    async def settle_difference(
        self,
        bridge_id: str,
        folder_id: Optional[str],
        apikey_id: Optional[str],
        reservation_cost: float,
        actual_cost: float,
        limits: dict,
    ) -> bool:
        """
        Adjust usage counters based on difference between reservation and actual cost.
        Usually actual_cost < reservation_cost, so adjustment is negative.
        """
        try:
            adjustment = actual_cost - reservation_cost

            bridge_period = limits.get("bridge", {}).get("reset_period", "monthly")
            bridge_start = limits.get("bridge", {}).get("start_date", datetime.utcnow())
            bridge_key = self._build_redis_key("bridge", bridge_id, bridge_period, bridge_start)

            folder_key = None
            if folder_id:
                folder_period = limits.get("folder", {}).get("reset_period", "monthly")
                folder_start = limits.get("folder", {}).get("start_date", datetime.utcnow())
                folder_key = self._build_redis_key("folder", folder_id, folder_period, folder_start)

            apikey_key = None
            if apikey_id:
                apikey_period = limits.get("apikey", {}).get("reset_period", "monthly")
                apikey_start = limits.get("apikey", {}).get("start_date", datetime.utcnow())
                apikey_key = self._build_redis_key("apikey", apikey_id, apikey_period, apikey_start)

            await self.redis.evalsha(
                self.settle_difference_script,
                3,
                bridge_key,
                folder_key or "none",
                apikey_key or "none",
                adjustment,
                adjustment,
                adjustment,
            )
            return True

        except Exception as e:
            logger.error(f"Error in settle_difference: {e}")
            return False

    async def get_current_usage(
        self,
        bridge_id: str,
        folder_id: Optional[str],
        apikey_id: Optional[str],
        limits: dict,
    ) -> dict:
        """Get current usage for all three entities."""
        try:
            bridge_period = limits.get("bridge", {}).get("reset_period", "monthly")
            bridge_start = limits.get("bridge", {}).get("start_date", datetime.utcnow())
            bridge_key = self._build_redis_key("bridge", bridge_id, bridge_period, bridge_start)

            folder_key = None
            if folder_id:
                folder_period = limits.get("folder", {}).get("reset_period", "monthly")
                folder_start = limits.get("folder", {}).get("start_date", datetime.utcnow())
                folder_key = self._build_redis_key("folder", folder_id, folder_period, folder_start)

            apikey_key = None
            if apikey_id:
                apikey_period = limits.get("apikey", {}).get("reset_period", "monthly")
                apikey_start = limits.get("apikey", {}).get("start_date", datetime.utcnow())
                apikey_key = self._build_redis_key("apikey", apikey_id, apikey_period, apikey_start)

            bridge_usage = float(await self.redis.get(bridge_key) or 0)
            folder_usage = float(await self.redis.get(folder_key or "none") or 0) if folder_id else 0
            apikey_usage = float(await self.redis.get(apikey_key or "none") or 0) if apikey_id else 0

            return {
                "bridge": bridge_usage,
                "folder": folder_usage,
                "apikey": apikey_usage,
            }

        except Exception as e:
            logger.error(f"Error getting current usage: {e}")
            return {"bridge": 0, "folder": 0, "apikey": 0}

    async def set_ttl_for_period(
        self,
        key: str,
        reset_period: str,
        start_date: datetime,
    ) -> bool:
        """Set TTL for a Redis key based on the reset period."""
        try:
            if reset_period == "monthly":
                if start_date.month == 12:
                    next_reset = start_date.replace(year=start_date.year + 1, month=1, day=1)
                else:
                    next_reset = start_date.replace(month=start_date.month + 1, day=1)
            elif reset_period == "weekly":
                next_reset = start_date + timedelta(weeks=1)
            elif reset_period == "daily":
                next_reset = start_date + timedelta(days=1)
            else:
                next_reset = start_date + timedelta(days=30)

            ttl_seconds = int((next_reset - datetime.utcnow()).total_seconds())
            if ttl_seconds > 0:
                await self.redis.expire(key, ttl_seconds)
            return True

        except Exception as e:
            logger.error(f"Error setting TTL: {e}")
            return False


usage_limits_service = UsageLimitsService()
