import json
import logging
from typing import Optional

from aio_pika import Message

from config import Config
from src.schemas.usage_limits import UsageEvent
from src.services.commonServices.queueService.baseQueue import BaseQueue

logger = logging.getLogger(__name__)


class UsageEventQueue(BaseQueue):
    """Queue service for publishing usage events to RabbitMQ."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self):
        queue_name = Config.METRICS_QUEUE_NAME or f"usage-events-{Config.ENVIRONMENT}"
        super().__init__(queue_name)
        logger.info(f"UsageEventQueue initialized with queue: {queue_name}")

    async def publish_usage_event(self, event: UsageEvent) -> bool:
        """
        Publish a usage event to the queue.
        This is a fire-and-forget operation - sub-millisecond.
        """
        try:
            if not await self._ensure_connection():
                logger.error("Failed to ensure RabbitMQ connection")
                return False

            event_dict = event.model_dump(mode="json")
            message_body = json.dumps(event_dict)

            message = Message(
                body=message_body.encode(),
                content_type="application/json",
                delivery_mode=2,  # Persistent
            )

            exchange = await self.channel.declare_exchange(
                "usage_events",
                "direct",
                durable=True,
            )

            queue = await self.channel.declare_queue(
                self.queue_name,
                durable=True,
            )

            await queue.bind(exchange, routing_key="usage_event")
            await exchange.publish(message, routing_key="usage_event")

            logger.debug(f"Published usage event: {event.request_id}")
            return True

        except Exception as e:
            logger.error(f"Error publishing usage event: {e}")
            return False

    async def publish_batch(self, events: list[UsageEvent]) -> bool:
        """Publish multiple usage events in batch."""
        try:
            for event in events:
                success = await self.publish_usage_event(event)
                if not success:
                    logger.warning(f"Failed to publish event: {event.request_id}")
            return True
        except Exception as e:
            logger.error(f"Error publishing batch: {e}")
            return False


usage_event_queue = UsageEventQueue()
