import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from redis.asyncio import Redis

from config import Config
from globals import logger

client = Redis.from_url(Config.REDIS_URI)

REDIS_PREFIX = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_"

LUA_CHECK_AND_RESERVE = """
local bridge_key = KEYS[1]
local folder_key = KEYS[2]
local apikey_key = KEYS[3]

local bridge_limit = tonumber(ARGV[1])
local folder_limit = tonumber(ARGV[2])
local apikey_limit = tonumber(ARGV[3])
local reservation = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local bridge_usage = tonumber(redis.call('GET', bridge_key) or 0)
local folder_usage = tonumber(redis.call('GET', folder_key) or 0)
local apikey_usage = tonumber(redis.call('GET', apikey_key) or 0)

if bridge_limit > 0 and (bridge_usage + reservation) > bridge_limit then
  return {0, "bridge", bridge_usage, bridge_limit}
end

if folder_limit > 0 and (folder_usage + reservation) > folder_limit then
  return {0, "folder", folder_usage, folder_limit}
end

if apikey_limit > 0 and (apikey_usage + reservation) > apikey_limit then
  return {0, "apikey", apikey_usage, apikey_limit}
end

redis.call('INCRBY', bridge_key, reservation)
redis.call('INCRBY', folder_key, reservation)
redis.call('INCRBY', apikey_key, reservation)

if ttl > 0 then
  redis.call('EXPIRE', bridge_key, ttl)
  redis.call('EXPIRE', folder_key, ttl)
  redis.call('EXPIRE', apikey_key, ttl)
end

return {1, "accepted", bridge_usage + reservation, bridge_limit}
"""


class UsageLimitsService:
    def __init__(self):
        self.lua_script = None
        self.limit_cache = {}
        self.cache_ttl = 300

    async def initialize(self):
        try:
            self.lua_script = await client.script_load(LUA_CHECK_AND_RESERVE)
            logger.info("Lua script loaded for usage limits")
        except Exception as e:
            logger.error(f"Failed to load Lua script: {str(e)}")
            raise

    async def get_limit_config(
        self,
        org_id: str,
        bridge_id: str,
        folder_id: Optional[str] = None,
        apikey_id: Optional[str] = None,
    ) -> dict:
        """
        Fetch limit configuration from MongoDB.
        Caches locally for 5 minutes.
        """
        cache_key = f"{org_id}:{bridge_id}:{folder_id}:{apikey_id}"

        if cache_key in self.limit_cache:
            cached_data, cached_time = self.limit_cache[cache_key]
            if (datetime.utcnow() - cached_time).total_seconds() < self.cache_ttl:
                return cached_data

        from models.mongo_connection import db

        bridge_doc = await db["bridges"].find_one(
            {"_id": bridge_id, "org_id": org_id}
        )

        if not bridge_doc:
            logger.warning(f"Bridge {bridge_id} not found for org {org_id}")
            return {
                "bridge_limit": 0,
                "bridge_reset_period": "monthly",
                "bridge_start_date": datetime.utcnow(),
                "bridge_hard_stop": True,
                "folder_limit": 0,
                "folder_reset_period": "monthly",
                "folder_start_date": datetime.utcnow(),
                "folder_hard_stop": True,
                "apikey_limit": 0,
                "apikey_reset_period": "monthly",
                "apikey_start_date": datetime.utcnow(),
                "apikey_hard_stop": True,
            }

        config = {
            "bridge_limit": float(bridge_doc.get("bridge_limit", 0)),
            "bridge_reset_period": bridge_doc.get("bridge_reset_period", "monthly"),
            "bridge_start_date": bridge_doc.get(
                "bridge_start_date", datetime.utcnow()
            ),
            "bridge_hard_stop": bridge_doc.get("bridge_hard_stop", True),
            "folder_limit": float(bridge_doc.get("folder_limit", 0)),
            "folder_reset_period": bridge_doc.get("folder_reset_period", "monthly"),
            "folder_start_date": bridge_doc.get(
                "folder_start_date", datetime.utcnow()
            ),
            "folder_hard_stop": bridge_doc.get("folder_hard_stop", True),
            "apikey_limit": float(bridge_doc.get("apikey_limit", 0)),
            "apikey_reset_period": bridge_doc.get("apikey_reset_period", "monthly"),
            "apikey_start_date": bridge_doc.get(
                "apikey_start_date", datetime.utcnow()
            ),
            "apikey_hard_stop": bridge_doc.get("apikey_hard_stop", True),
        }

        self.limit_cache[cache_key] = (config, datetime.utcnow())
        return config

    def _calculate_period_end(
        self, start_date: datetime, period: str
    ) -> datetime:
        """Calculate when the current period ends."""
        if period == "daily":
            return start_date + timedelta(days=1)
        elif period == "weekly":
            return start_date + timedelta(weeks=1)
        elif period == "monthly":
            if start_date.month == 12:
                return start_date.replace(year=start_date.year + 1, month=1)
            else:
                return start_date.replace(month=start_date.month + 1)
        return start_date + timedelta(days=30)

    def _build_redis_key(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
        period: str,
        start_date: datetime,
    ) -> tuple[str, int]:
        """
        Build Redis key and calculate TTL.
        Key format: quota:{org_id}:{entity_type}:{entity_id}:{period}:{start_date_iso}
        """
        start_iso = start_date.isoformat()
        key = f"{REDIS_PREFIX}quota:{org_id}:{entity_type}:{entity_id}:{period}:{start_iso}"

        period_end = self._calculate_period_end(start_date, period)
        ttl = int((period_end - datetime.utcnow()).total_seconds())
        ttl = max(ttl, 0)

        return key, ttl

    async def check_and_reserve(
        self,
        org_id: str,
        bridge_id: str,
        folder_id: Optional[str],
        apikey_id: Optional[str],
        estimated_cost: float,
    ) -> dict:
        """
        Check if request is under limits and reserve the estimated cost.
        Returns:
        {
            'allowed': bool,
            'reason': str or None,
            'limit_type': str or None (bridge/folder/apikey),
            'current_usage': float or None,
            'limit_value': float or None,
        }
        """
        try:
            config = await self.get_limit_config(
                org_id, bridge_id, folder_id, apikey_id
            )

            bridge_key, bridge_ttl = self._build_redis_key(
                org_id,
                "bridge",
                bridge_id,
                config["bridge_reset_period"],
                config["bridge_start_date"],
            )

            folder_key = ""
            folder_ttl = 0
            if folder_id:
                folder_key, folder_ttl = self._build_redis_key(
                    org_id,
                    "folder",
                    folder_id,
                    config["folder_reset_period"],
                    config["folder_start_date"],
                )

            apikey_key = ""
            apikey_ttl = 0
            if apikey_id:
                apikey_key, apikey_ttl = self._build_redis_key(
                    org_id,
                    "apikey",
                    apikey_id,
                    config["apikey_reset_period"],
                    config["apikey_start_date"],
                )

            max_ttl = max(bridge_ttl, folder_ttl, apikey_ttl)

            result = await client.evalsha(
                self.lua_script,
                3,
                bridge_key,
                folder_key,
                apikey_key,
                config["bridge_limit"],
                config["folder_limit"],
                config["apikey_limit"],
                estimated_cost,
                max_ttl,
            )

            if result[0] == 1:
                return {
                    "allowed": True,
                    "reason": None,
                    "limit_type": None,
                    "current_usage": None,
                    "limit_value": None,
                }
            else:
                limit_type = result[1]
                current_usage = float(result[2])
                limit_value = float(result[3])

                return {
                    "allowed": False,
                    "reason": f"{limit_type.capitalize()} limit exceeded",
                    "limit_type": limit_type,
                    "current_usage": current_usage,
                    "limit_value": limit_value,
                }

        except Exception as e:
            logger.error(f"Error in check_and_reserve: {str(e)}")
            return {
                "allowed": False,
                "reason": "Internal error checking limits",
                "limit_type": None,
                "current_usage": None,
                "limit_value": None,
            }

    async def settle_usage(
        self,
        org_id: str,
        bridge_id: str,
        folder_id: Optional[str],
        apikey_id: Optional[str],
        reservation_cost: float,
        actual_cost: float,
    ) -> bool:
        """
        Adjust usage after actual cost is known.
        Difference = actual_cost - reservation_cost (usually negative).
        """
        try:
            config = await self.get_limit_config(
                org_id, bridge_id, folder_id, apikey_id
            )

            adjustment = actual_cost - reservation_cost

            bridge_key, _ = self._build_redis_key(
                org_id,
                "bridge",
                bridge_id,
                config["bridge_reset_period"],
                config["bridge_start_date"],
            )

            if adjustment != 0:
                await client.incrbyfloat(bridge_key, adjustment)

            if folder_id:
                folder_key, _ = self._build_redis_key(
                    org_id,
                    "folder",
                    folder_id,
                    config["folder_reset_period"],
                    config["folder_start_date"],
                )
                if adjustment != 0:
                    await client.incrbyfloat(folder_key, adjustment)

            if apikey_id:
                apikey_key, _ = self._build_redis_key(
                    org_id,
                    "apikey",
                    apikey_id,
                    config["apikey_reset_period"],
                    config["apikey_start_date"],
                )
                if adjustment != 0:
                    await client.incrbyfloat(apikey_key, adjustment)

            return True

        except Exception as e:
            logger.error(f"Error in settle_usage: {str(e)}")
            return False

    async def get_current_usage(
        self,
        org_id: str,
        bridge_id: str,
        folder_id: Optional[str] = None,
        apikey_id: Optional[str] = None,
    ) -> dict:
        """Get current usage for all three entities."""
        try:
            config = await self.get_limit_config(
                org_id, bridge_id, folder_id, apikey_id
            )

            bridge_key, _ = self._build_redis_key(
                org_id,
                "bridge",
                bridge_id,
                config["bridge_reset_period"],
                config["bridge_start_date"],
            )

            bridge_usage = float(await client.get(bridge_key) or 0)

            folder_usage = 0
            if folder_id:
                folder_key, _ = self._build_redis_key(
                    org_id,
                    "folder",
                    folder_id,
                    config["folder_reset_period"],
                    config["folder_start_date"],
                )
                folder_usage = float(await client.get(folder_key) or 0)

            apikey_usage = 0
            if apikey_id:
                apikey_key, _ = self._build_redis_key(
                    org_id,
                    "apikey",
                    apikey_id,
                    config["apikey_reset_period"],
                    config["apikey_start_date"],
                )
                apikey_usage = float(await client.get(apikey_key) or 0)

            return {
                "bridge": {
                    "usage": bridge_usage,
                    "limit": config["bridge_limit"],
                    "period": config["bridge_reset_period"],
                },
                "folder": {
                    "usage": folder_usage,
                    "limit": config["folder_limit"],
                    "period": config["folder_reset_period"],
                }
                if folder_id
                else None,
                "apikey": {
                    "usage": apikey_usage,
                    "limit": config["apikey_limit"],
                    "period": config["apikey_reset_period"],
                }
                if apikey_id
                else None,
            }

        except Exception as e:
            logger.error(f"Error getting current usage: {str(e)}")
            return {}

    def clear_limit_cache(self):
        """Clear the in-process limit cache."""
        self.limit_cache.clear()


usage_limits_service = UsageLimitsService()
