import asyncio
import logging
from datetime import datetime, timedelta

from redis.asyncio import Redis

from config import Config
from src.services.usage_limits.timescale_worker import timescale_worker

logger = logging.getLogger(__name__)

RECONCILIATION_INTERVAL_SECONDS = 300  # 5 minutes
DRIFT_THRESHOLD = 0.01  # $0.01 difference triggers correction


class ReconciliationJob:
    """
    Background job that reconciles Redis usage counters with TimescaleDB.
    Runs every 5 minutes to detect and correct drift.
    """

    def __init__(self):
        self.redis = Redis.from_url(Config.REDIS_URI)
        self.running = False

    async def start(self):
        """Start the reconciliation job."""
        self.running = True
        asyncio.create_task(self._reconciliation_loop())
        logger.info("Reconciliation job started")

    async def stop(self):
        """Stop the reconciliation job."""
        self.running = False
        logger.info("Reconciliation job stopped")

    async def _reconciliation_loop(self):
        """Main reconciliation loop."""
        while self.running:
            try:
                await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)
                await self._reconcile_all_entities()
            except Exception as e:
                logger.error(f"Error in reconciliation loop: {e}")

    async def _reconcile_all_entities(self):
        """Reconcile all tracked entities."""
        try:
            pattern = "quota:*:bridge:*"
            cursor = 0
            keys_to_check = []

            while True:
                cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
                keys_to_check.extend(keys)
                if cursor == 0:
                    break

            logger.info(f"Reconciling {len(keys_to_check)} bridge entities")

            for key in keys_to_check:
                await self._reconcile_single_entity(key)

        except Exception as e:
            logger.error(f"Error reconciling all entities: {e}")

    async def _reconcile_single_entity(self, redis_key: str):
        """
        Reconcile a single entity by comparing Redis vs TimescaleDB.
        If drift exceeds threshold, correct Redis.
        """
        try:
            redis_usage = float(await self.redis.get(redis_key) or 0)

            parts = redis_key.split(":")
            if len(parts) < 4:
                return

            entity_type = parts[2]
            entity_id = parts[3]

            now = datetime.utcnow()
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            summary = await timescale_worker.get_usage_summary(
                org_id="*",
                entity_type=entity_type,
                entity_id=entity_id,
                start_date=period_start,
                end_date=now,
            )

            timescale_usage = summary.get("total_cost", 0)
            drift = abs(redis_usage - timescale_usage)

            if drift > DRIFT_THRESHOLD:
                logger.warning(
                    f"Drift detected for {entity_type}:{entity_id}. "
                    f"Redis: {redis_usage}, TimescaleDB: {timescale_usage}, Drift: {drift}"
                )

                await self.redis.set(redis_key, timescale_usage)
                logger.info(f"Corrected {entity_type}:{entity_id} to {timescale_usage}")

        except Exception as e:
            logger.error(f"Error reconciling entity {redis_key}: {e}")

    async def rebuild_from_timescale(
        self,
        entity_type: str,
        entity_id: str,
        org_id: str,
        redis_key: str,
    ) -> float:
        """
        Rebuild a Redis counter from TimescaleDB.
        Used when Redis crashes and we need to recover state.
        """
        try:
            now = datetime.utcnow()
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            summary = await timescale_worker.get_usage_summary(
                org_id=org_id,
                entity_type=entity_type,
                entity_id=entity_id,
                start_date=period_start,
                end_date=now,
            )

            total_cost = summary.get("total_cost", 0)
            await self.redis.set(redis_key, total_cost)

            logger.info(f"Rebuilt {entity_type}:{entity_id} from TimescaleDB: {total_cost}")
            return total_cost

        except Exception as e:
            logger.error(f"Error rebuilding from TimescaleDB: {e}")
            return 0


reconciliation_job = ReconciliationJob()
