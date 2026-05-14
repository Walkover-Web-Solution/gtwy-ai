import logging
from typing import Optional

from redis.asyncio import Redis

from config import Config

logger = logging.getLogger(__name__)

redis_client = Redis.from_url(Config.REDIS_URI)
FEATURE_FLAG_PREFIX = f"feature_flag:{Config.ENVIRONMENT}:"


class FeatureFlags:
    """
    Feature flag service for gradual rollout of usage limits.
    Stores flags in Redis for fast access.
    """

    STEP_1_REQUEST_ID = "step_1_request_id"
    STEP_2_TIMESCALE_EVENTS = "step_2_timescale_events"
    STEP_3_WEBHOOK_THRESHOLD = "step_3_webhook_threshold"
    STEP_4_LUA_CHECK_AND_RESERVE = "step_4_lua_check_and_reserve"
    STEP_5_TIMESCALE_REBUILD = "step_5_timescale_rebuild"
    STEP_6_STOP_MONGODB_USAGE = "step_6_stop_mongodb_usage"
    STEP_7_HARD_STOP_CONFIG = "step_7_hard_stop_config"

    def __init__(self):
        self.redis = redis_client

    async def is_enabled(self, flag_name: str, org_id: Optional[str] = None) -> bool:
        """Check if a feature flag is enabled for an org."""
        try:
            if org_id:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}:{org_id}"
            else:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}"

            value = await self.redis.get(key)
            return value == b"true" if value else False

        except Exception as e:
            logger.error(f"Error checking feature flag {flag_name}: {e}")
            return False

    async def enable(self, flag_name: str, org_id: Optional[str] = None, ttl: int = 86400) -> bool:
        """Enable a feature flag."""
        try:
            if org_id:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}:{org_id}"
            else:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}"

            await self.redis.set(key, "true", ex=ttl)
            logger.info(f"Enabled feature flag: {flag_name} (org: {org_id})")
            return True

        except Exception as e:
            logger.error(f"Error enabling feature flag {flag_name}: {e}")
            return False

    async def disable(self, flag_name: str, org_id: Optional[str] = None) -> bool:
        """Disable a feature flag."""
        try:
            if org_id:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}:{org_id}"
            else:
                key = f"{FEATURE_FLAG_PREFIX}{flag_name}"

            await self.redis.delete(key)
            logger.info(f"Disabled feature flag: {flag_name} (org: {org_id})")
            return True

        except Exception as e:
            logger.error(f"Error disabling feature flag {flag_name}: {e}")
            return False

    async def enable_for_percentage(
        self,
        flag_name: str,
        percentage: int,
        org_ids: list[str],
    ) -> bool:
        """
        Enable a feature flag for a percentage of orgs.
        Useful for gradual rollout.
        """
        try:
            count = max(1, len(org_ids) * percentage // 100)
            enabled_orgs = org_ids[:count]

            for org_id in enabled_orgs:
                await self.enable(flag_name, org_id)

            logger.info(f"Enabled {flag_name} for {len(enabled_orgs)} orgs ({percentage}%)")
            return True

        except Exception as e:
            logger.error(f"Error enabling flag for percentage: {e}")
            return False


feature_flags = FeatureFlags()
