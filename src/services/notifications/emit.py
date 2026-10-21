"""Publishes notification events to the hub (a Node consumer on NOTIFICATION_QUEUE_NAME)."""

import uuid
from datetime import UTC, datetime

from src.configs.notification_events import NOTIFICATION_EVENTS
from src.services.commonServices.queueService.queueNotificationService import notification_queue_obj
from src.services.utils.logger import logger


def build_envelope(
    event_type,
    org_id=None,
    *,
    title,
    message,
    agent_id=None,
    data=None,
    severity=None,
    audience=None,
    dedupe_key=None,
):
    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "org_id": str(org_id) if org_id is not None else None,
        "agent_id": str(agent_id) if agent_id is not None else None,
        "title": title,
        "message": message,
        "data": data or {},
        "dedupe_key": dedupe_key,
        "source": "python",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if severity:
        envelope["severity"] = severity
    if audience:
        envelope["audience"] = audience
    return envelope


async def emit_event(event_type, org_id=None, **kwargs):
    """Publish one notification event. Never raises: a notification must not break the caller.

    Returns True when the event was handed to the queue.
    """
    try:
        if event_type not in NOTIFICATION_EVENTS:
            logger.error(f"emit_event: unknown event_type {event_type!r}")
            return False
        if notification_queue_obj is None:
            logger.warning(f"emit_event: NOTIFICATION_QUEUE_NAME is not set, dropping {event_type}")
            return False

        envelope = build_envelope(event_type, org_id, **kwargs)
        await notification_queue_obj.create_queue_if_not_exists()
        return await notification_queue_obj.publish_message(envelope)
    except Exception as error:
        logger.error(f"emit_event failed for {event_type}: {error}")
        return False
