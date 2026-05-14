import json
import logging
from datetime import datetime
from typing import Optional

import aio_pika

from config import Config
from globals import logger

connection = None
channel = None
exchange = None


async def initialize_producer():
    """Initialize RabbitMQ connection and channel."""
    global connection, channel, exchange

    try:
        connection = await aio_pika.connect_robust(Config.QUEUE_CONNECTIONURL)
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            "usage_events", aio_pika.ExchangeType.DIRECT, durable=True
        )
        logger.info("Usage events producer initialized")
    except Exception as e:
        logger.error(f"Failed to initialize usage events producer: {str(e)}")
        raise


async def publish_usage_event(
    request_id: str,
    org_id: str,
    bridge_id: str,
    service: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    status: str = "success",
    folder_id: Optional[str] = None,
    apikey_id: Optional[str] = None,
    reservation_cost: Optional[float] = None,
    actual_cost: Optional[float] = None,
) -> bool:
    """
    Publish a usage event to RabbitMQ.
    This is fire-and-forget — the user's request doesn't wait for this.
    """
    try:
        if not channel or not exchange:
            logger.error("Producer not initialized")
            return False

        event = {
            "request_id": request_id,
            "org_id": org_id,
            "bridge_id": bridge_id,
            "folder_id": folder_id,
            "apikey_id": apikey_id,
            "service": service,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            "reservation_cost": reservation_cost,
            "actual_cost": actual_cost,
        }

        message = aio_pika.Message(
            body=json.dumps(event).encode("utf-8"),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )

        await exchange.publish(message, routing_key="usage_event")
        logger.debug(f"Published usage event: {request_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to publish usage event: {str(e)}")
        return False


async def close_producer():
    """Close RabbitMQ connection."""
    global connection

    try:
        if connection:
            await connection.close()
            logger.info("Usage events producer closed")
    except Exception as e:
        logger.error(f"Error closing producer: {str(e)}")
