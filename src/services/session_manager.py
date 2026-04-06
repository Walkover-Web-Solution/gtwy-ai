"""
Redis-backed coordination layer for multi-worker WorkflowSession management.

Session metadata key:  AIMIDDLEWARE_workflow_session:{run_id}  (JSON, 1h TTL)
Input channel:         AIMIDDLEWARE_workflow_input:{run_id}     (WS worker → session worker)
Event channel:         AIMIDDLEWARE_workflow_event:{run_id}     (session worker → WS worker)

The WorkflowSession object stays in-process. Only lightweight routing uses Redis.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from redis.asyncio import Redis

from config import Config
from globals import logger
from src.services.cache_service import client as _redis_cmd  # Reuse existing Redis client

_PREFIX = "AIMIDDLEWARE_"
_SESSION_TTL = 3600  # 1 hour safety net; deleted immediately on completion


def _session_key(run_id: str) -> str:
    return f"{_PREFIX}workflow_session:{run_id}"

def _input_channel(run_id: str) -> str:
    return f"{_PREFIX}workflow_input:{run_id}"

def _event_channel(run_id: str) -> str:
    return f"{_PREFIX}workflow_event:{run_id}"


async def register_session_in_redis(run_id: str) -> None:
    try:
        await _redis_cmd.set(_session_key(run_id), json.dumps({"run_id": run_id, "status": "active"}), ex=_SESSION_TTL)
        logger.info(f"[SessionManager] registered run_id={run_id}")
    except Exception as exc:
        logger.error(f"[SessionManager] register_session_in_redis: {exc}")


async def create_pending_session_in_redis(run_id: str) -> bool:
    """
    Create a placeholder 'pending' session when WebSocket connects early.
    Returns True if created, False if already exists.
    """
    try:
        # Only create if doesn't exist (NX = Not eXists)
        result = await _redis_cmd.set(
            _session_key(run_id),
            json.dumps({"run_id": run_id, "status": "pending"}),
            ex=_SESSION_TTL,
            nx=True,  # Only set if key doesn't exist
        )
        if result:
            logger.info(f"[SessionManager] created pending session for run_id={run_id}")
        return result is not None
    except Exception as exc:
        logger.error(f"[SessionManager] create_pending_session_in_redis: {exc}")
        return False


async def session_exists_in_redis(run_id: str) -> bool:
    try:
        return await _redis_cmd.get(_session_key(run_id)) is not None
    except Exception as exc:
        logger.error(f"[SessionManager] session_exists_in_redis: {exc}")
        return False


async def unregister_session_from_redis(run_id: str) -> None:
    try:
        await _redis_cmd.delete(_session_key(run_id))
        logger.info(f"[SessionManager] unregistered run_id={run_id}")
    except Exception as exc:
        logger.error(f"[SessionManager] unregister_session_from_redis: {exc}")


async def publish_human_input(run_id: str, answer: Any) -> None:
    payload = json.dumps({"run_id": run_id, "answer": answer})
    try:
        await _redis_cmd.publish(_input_channel(run_id), payload)
    except Exception as exc:
        logger.error(f"[SessionManager] publish_human_input: {exc}")


async def subscribe_to_human_input(run_id: str, local_queue: asyncio.Queue) -> None:
    """
    Blocks until an answer arrives on the Redis input channel, then puts it
    into local_queue so the existing asyncio.wait_for(queue.get()) path works.
    Uses a dedicated Redis connection (required by redis-py pubsub).
    """
    pubsub_client: Redis = Redis.from_url(Config.REDIS_URI, decode_responses=True)
    pubsub = pubsub_client.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(_input_channel(run_id))
        deadline = asyncio.get_event_loop().time() + 600
        while asyncio.get_event_loop().time() < deadline:
            message = await pubsub.get_message(timeout=1.0)
            if message and message.get("type") == "message":
                data = json.loads(message["data"])
                await local_queue.put(data.get("answer"))
                return
        logger.warning(f"[SessionManager] subscribe_to_human_input timed out for run_id={run_id}")
    except Exception as exc:
        logger.error(f"[SessionManager] subscribe_to_human_input: {exc}")
    finally:
        await pubsub.unsubscribe(_input_channel(run_id))
        await pubsub.aclose()
        await pubsub_client.aclose()


async def publish_workflow_event(run_id: str, event: str, node: str, data: dict) -> None:
    payload = json.dumps({"event": event, "node": node, "run_id": run_id, "data": data})
    try:
        await _redis_cmd.publish(_event_channel(run_id), payload)
    except Exception as exc:
        logger.error(f"[SessionManager] publish_workflow_event: {exc}")


async def subscribe_to_workflow_events(run_id: str, websocket: Any, stop_event: asyncio.Event) -> None:
    """
    Relay events published by the session worker to the frontend WebSocket.
    Runs until stop_event is set (disconnect or "done"/"error" event received).
    """
    pubsub_client: Redis = Redis.from_url(Config.REDIS_URI, decode_responses=True)
    pubsub = pubsub_client.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(_event_channel(run_id))
        while not stop_event.is_set():
            message = await pubsub.get_message(timeout=0.1)
            if message and message.get("type") == "message":
                try:
                    payload = json.loads(message["data"])
                    await websocket.send_json(payload)
                    if payload.get("event") == "error":
                        stop_event.set()
                    elif payload.get("event") == "done":
                        # Keep relay alive when workflow is paused waiting for
                        # human input (workflow_status == "waiting"). Only stop
                        # when the workflow truly completes.
                        if payload.get("data", {}).get("workflow_status") != "waiting":
                            stop_event.set()
                except Exception as ws_exc:
                    logger.error(f"[SessionManager] WS send error for run_id={run_id}: {ws_exc}")
                    stop_event.set()
    except Exception as exc:
        logger.error(f"[SessionManager] subscribe_to_workflow_events: {exc}")
    finally:
        await pubsub.unsubscribe(_event_channel(run_id))
        await pubsub.aclose()
        await pubsub_client.aclose()


__all__ = [
    "register_session_in_redis",
    "create_pending_session_in_redis",
    "session_exists_in_redis",
    "unregister_session_from_redis",
    "publish_human_input",
    "subscribe_to_human_input",
    "publish_workflow_event",
    "subscribe_to_workflow_events",
]
