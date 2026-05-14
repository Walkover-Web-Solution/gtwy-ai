import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config import Config
from models.Timescale.usage_events import Base, UsageEventRecord
from src.schemas.usage_limits import UsageEvent
from src.services.commonServices.queueService.baseQueue import BaseQueue

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
BATCH_TIMEOUT_SECONDS = 1


class TimescaleWorker(BaseQueue):
    """Worker that consumes usage events from RabbitMQ and writes to TimescaleDB."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self):
        queue_name = Config.METRICS_QUEUE_NAME or f"usage-events-{Config.ENVIRONMENT}"
        super().__init__(queue_name)

        self.db_url = Config.TIMESCALE_SERVICE_URL
        self.engine = None
        self.async_session = None
        self.event_buffer = []
        self.buffer_lock = asyncio.Lock()
        logger.info("TimescaleWorker initialized")

    async def initialize(self):
        """Initialize database connection."""
        try:
            self.engine = create_async_engine(self.db_url, echo=False)
            self.async_session = sessionmaker(
                self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            logger.info("TimescaleDB connection initialized")
        except Exception as e:
            logger.error(f"Failed to initialize TimescaleDB: {e}")
            raise

    async def process_messages(self, message):
        """Process a single message from the queue."""
        try:
            body = json.loads(message.body.decode())
            event = UsageEvent(**body)

            async with self.buffer_lock:
                self.event_buffer.append(event)

                if len(self.event_buffer) >= BATCH_SIZE:
                    await self._flush_buffer()

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _flush_buffer(self) -> bool:
        """Write buffered events to TimescaleDB in a single transaction."""
        if not self.event_buffer:
            return True

        try:
            async with self.async_session() as session:
                async with session.begin():
                    for event in self.event_buffer:
                        record = UsageEventRecord(
                            request_id=event.request_id,
                            org_id=event.org_id,
                            bridge_id=event.bridge_id,
                            folder_id=event.folder_id,
                            apikey_id=event.apikey_id,
                            service=event.service,
                            model=event.model,
                            tokens_in=event.tokens_in,
                            tokens_out=event.tokens_out,
                            cost_usd=event.cost_usd,
                            status=event.status,
                            timestamp=event.timestamp,
                        )
                        session.add(record)

                    await session.commit()

            logger.info(f"Flushed {len(self.event_buffer)} events to TimescaleDB")
            self.event_buffer = []
            return True

        except Exception as e:
            logger.error(f"Error flushing buffer to TimescaleDB: {e}")
            return False

    async def consume_and_batch(self):
        """
        Consume messages from RabbitMQ and batch them.
        Flushes when either BATCH_SIZE is reached or BATCH_TIMEOUT_SECONDS passes.
        """
        try:
            if not await self._ensure_connection():
                logger.error("Failed to connect to RabbitMQ")
                return

            queue = await self.channel.declare_queue(self.queue_name, durable=True)
            await queue.consume(
                lambda message: self._message_handler_wrapper(message, self.process_messages)
            )

            logger.info(f"Started consuming usage events from {self.queue_name}")

            while True:
                async with self.buffer_lock:
                    if self.event_buffer and len(self.event_buffer) > 0:
                        await self._flush_buffer()

                await asyncio.sleep(BATCH_TIMEOUT_SECONDS)

        except Exception as e:
            logger.error(f"Error in consume_and_batch: {e}")

    async def get_usage_summary(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> dict:
        """Query TimescaleDB for usage summary."""
        try:
            async with self.async_session() as session:
                if entity_type == "bridge":
                    query = text(
                        """
                        SELECT
                            SUM(cost_usd) as total_cost,
                            SUM(tokens_in) as total_tokens_in,
                            SUM(tokens_out) as total_tokens_out,
                            COUNT(*) as request_count
                        FROM usage_events
                        WHERE org_id = :org_id
                            AND bridge_id = :entity_id
                            AND timestamp >= :start_date
                            AND timestamp < :end_date
                        """
                    )
                elif entity_type == "folder":
                    query = text(
                        """
                        SELECT
                            SUM(cost_usd) as total_cost,
                            SUM(tokens_in) as total_tokens_in,
                            SUM(tokens_out) as total_tokens_out,
                            COUNT(*) as request_count
                        FROM usage_events
                        WHERE org_id = :org_id
                            AND folder_id = :entity_id
                            AND timestamp >= :start_date
                            AND timestamp < :end_date
                        """
                    )
                elif entity_type == "apikey":
                    query = text(
                        """
                        SELECT
                            SUM(cost_usd) as total_cost,
                            SUM(tokens_in) as total_tokens_in,
                            SUM(tokens_out) as total_tokens_out,
                            COUNT(*) as request_count
                        FROM usage_events
                        WHERE org_id = :org_id
                            AND apikey_id = :entity_id
                            AND timestamp >= :start_date
                            AND timestamp < :end_date
                        """
                    )
                else:
                    return {}

                result = await session.execute(
                    query,
                    {
                        "org_id": org_id,
                        "entity_id": entity_id,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )

                row = result.fetchone()
                if row:
                    return {
                        "total_cost": float(row[0] or 0),
                        "total_tokens_in": int(row[1] or 0),
                        "total_tokens_out": int(row[2] or 0),
                        "request_count": int(row[3] or 0),
                    }
                return {"total_cost": 0, "total_tokens_in": 0, "total_tokens_out": 0, "request_count": 0}

        except Exception as e:
            logger.error(f"Error querying usage summary: {e}")
            return {}


timescale_worker = TimescaleWorker()
