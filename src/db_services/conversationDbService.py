import time as _time
from datetime import datetime

from sqlalchemy import and_, text

from globals import logger
from models.index import combined_models as models
from models.postgres.pg_models import (
    ConversationLog,
    system_prompt_versionings,
    user_bridge_config_history,
)
from src.services.utils.time import log_slow_call, SLOW_CALL_THRESHOLDS

pg = models["pg"]


async def find_conversation_logs(org_id, thread_id, sub_thread_id, bridge_id):
    """
    Find conversation logs from the new consolidated conversation_logs table

    Args:
        org_id: Organization ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        bridge_id: Bridge ID

    Returns:
        List of conversation logs formatted for response
    """
    try:
        session = pg["session"]()
        _t = _time.time()
        logs = (
            session.query(ConversationLog)
            .filter(
                and_(
                    ConversationLog.org_id == org_id,
                    ConversationLog.thread_id == thread_id,
                    ConversationLog.sub_thread_id == sub_thread_id,
                    ConversationLog.bridge_id == bridge_id,
                    ConversationLog.status,  # Only successful conversations
                )
            )
            .order_by(ConversationLog.created_at.desc())
            .limit(3)
            .all()
        )
        log_slow_call("PG query find_conversation_logs", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])
        

        # Convert logs to conversation format expected by the application
        conversations = []
        for log in reversed(logs):
            # Add user message
            if log.user:
                conversations.append(
                    {
                        "content": log.user,
                        "role": "user",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": None,
                        "is_reset": False,
                        "tools_call_data": log.tools_call_data,
                        "error": "",
                        "user_urls": log.user_urls or [],
                    }
                )

            # Add tools_call if present
            if log.tools_call_data:
                conversations.append(
                    {
                        "content": "",
                        "role": "tools_call",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": {},
                        "is_reset": False,
                        "tools_call_data": log.tools_call_data,
                        "error": "",
                        "urls": [],
                    }
                )

            # Add assistant message
            if log.chatbot_message or log.llm_message:
                conversations.append(
                    {
                        "content": log.chatbot_message or log.llm_message,
                        "role": "assistant",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": {},
                        "is_reset": False,
                        "tools_call_data": None,
                        "error": "",
                        "llm_urls": log.llm_urls or [],
                    }
                )

        return conversations
    except Exception as e:
        logger.error(f"Error in finding conversation logs: {str(e)}")
        return []
    finally:
        session.close()


async def find_completed_batch_conversations(org_id, thread_id, sub_thread_id, bridge_id, limit=3):
    """
    Find only completed (non-queued) conversation logs for batch API thread history.
    Excludes conversations with batch_data status='queued'.

    Args:
        org_id: Organization ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        bridge_id: Bridge ID
        limit: Maximum number of conversation pairs to fetch (default 3)

    Returns:
        List of conversation logs formatted for response (only completed conversations)
    """
    try:
        session = pg["session"]()

        # Query for completed conversations only
        # Exclude logs where batch_data->>'status' = 'queued'
        _t = _time.time()
        logs = (
            session.query(ConversationLog)
            .filter(
                and_(
                    ConversationLog.org_id == org_id,
                    ConversationLog.thread_id == thread_id,
                    ConversationLog.sub_thread_id == sub_thread_id,
                    ConversationLog.bridge_id == bridge_id,
                    ConversationLog.status == True,  # Only successful conversations
                    # Exclude queued batch conversations
                    # Either batch_data is null OR batch_data->>'status' != 'queued'
                    text(
                        "(batch_data IS NULL OR batch_data->>'status' IS NULL OR batch_data->>'status' != 'queued')"
                    )
                )
            )
            .order_by(ConversationLog.created_at.desc())
            .limit(limit)
            .all()
        )
        log_slow_call("PG query find_completed_batch_conversations", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])

        # Convert logs to conversation format expected by the application
        conversations = []
        for log in reversed(logs):
            # Add user message
            if log.user:
                conversations.append(
                    {
                        "content": log.user,
                        "role": "user",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": None,
                        "is_reset": False,
                        "tools_call_data": log.tools_call_data,
                        "error": "",
                        "user_urls": log.user_urls or [],
                    }
                )

            # Add tools_call if present
            if log.tools_call_data:
                conversations.append(
                    {
                        "content": "",
                        "role": "tools_call",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": {},
                        "is_reset": False,
                        "tools_call_data": log.tools_call_data,
                        "error": "",
                    }
                )

            # Add assistant message
            if log.chatbot_message:
                conversations.append(
                    {
                        "content": log.chatbot_message,
                        "role": "assistant",
                        "createdAt": log.created_at,
                        "id": log.id,
                        "function": None,
                        "is_reset": False,
                        "tools_call_data": None,
                        "error": log.error if log.error else "",
                        "llm_urls": log.llm_urls or [],
                    }
                )

        logger.info(f"Found {len(logs)} completed conversations for batch thread history")
        return conversations
    except Exception as e:
        logger.error(f"Error in finding completed batch conversations: {str(e)}")
        return []
    finally:
        session.close()


async def storeSystemPrompt(prompt, org_id, bridge_id):
    session = pg["session"]()
    try:
        new_prompt = system_prompt_versionings(
            system_prompt=prompt,
            org_id=org_id,
            bridge_id=bridge_id,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session.add(new_prompt)
        _t = _time.time()
        session.commit()
        log_slow_call("PG commit storeSystemPrompt", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])
        return {"id": new_prompt.id}
    except Exception as error:
        session.rollback()
        logger.error(f"Error in storing system prompt: {str(error)}")
        raise error
    finally:
        session.close()


async def find_rerun_logs(org_id, message_ids=None, bridge_id=None, thread_id=None, sub_thread_id=None, limit=6):
    """
    Fetch conversation logs for rerun — either by explicit message_ids or by thread.

    By message_ids:  returns (logs_map, [])
        logs_map = {message_id: log_dict, ...}
    By thread:       returns (logs_map, conversations)
        logs_map has a single entry for the most recent message.
        conversations is the last `limit` entries (oldest-first) for history context.
    """
    session = pg["session"]()
    try:
        query = session.query(ConversationLog).filter(ConversationLog.org_id == org_id)

        _t = _time.time()
        if message_ids:
            logs = query.filter(ConversationLog.message_id.in_(message_ids)).all()
        else:
            logs = (
                query.filter(
                    and_(
                        ConversationLog.bridge_id == bridge_id,
                        ConversationLog.thread_id == thread_id,
                        ConversationLog.sub_thread_id == sub_thread_id,
                        ConversationLog.status,
                    )
                )
                .order_by(ConversationLog.created_at.desc())
                .limit(limit)
                .all()
            )
        log_slow_call("PG query find_rerun_logs", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])

        if not logs:
            return {}, []

        def _log_to_dict(log):
            return {
                "message_id": log.message_id,
                "bridge_id": log.bridge_id,
                "org_id": log.org_id,
                "version_id": log.version_id,
                "thread_id": log.thread_id,
                "sub_thread_id": log.sub_thread_id,
                "user": log.user,
                "variables": log.variables or {},
                "user_urls": log.user_urls or [],
                "service": log.service,
                "model": log.model,
            }

        # message_ids path — return map of all found logs
        if message_ids:
            return {log.message_id: _log_to_dict(log) for log in logs}, []

        # thread path — most recent log (index 0) is the rerun target
        logs_map = {logs[0].message_id: _log_to_dict(logs[0])}

        # Build conversations oldest-first (same format as find_conversation_logs)
        conversations = []
        for log in reversed(logs):
            if log.user:
                conversations.append({
                    "content": log.user,
                    "role": "user",
                    "createdAt": log.created_at,
                    "id": log.id,
                    "function": None,
                    "is_reset": False,
                    "tools_call_data": log.tools_call_data,
                    "error": "",
                    "user_urls": log.user_urls or [],
                })
            if log.tools_call_data:
                conversations.append({
                    "content": "",
                    "role": "tools_call",
                    "createdAt": log.created_at,
                    "id": log.id,
                    "function": {},
                    "is_reset": False,
                    "tools_call_data": log.tools_call_data,
                    "error": "",
                    "urls": [],
                })
            if log.chatbot_message or log.llm_message:
                conversations.append({
                    "content": log.chatbot_message or log.llm_message,
                    "role": "assistant",
                    "createdAt": log.created_at,
                    "id": log.id,
                    "function": {},
                    "is_reset": False,
                    "tools_call_data": None,
                    "error": "",
                    "llm_urls": log.llm_urls or [],
                })

        return logs_map, conversations
    except Exception as e:
        logger.error(f"Error fetching rerun logs: {str(e)}")
        return {}, []
    finally:
        session.close()


async def update_conversation_log(message_id, org_id, update_data):
    """
    Update an existing conversation log row by message_id and org_id.

    Args:
        message_id: The message ID to update
        org_id: Organization ID for safety
        update_data: Dict of column names -> new values

    Returns:
        True if a row was updated, False otherwise
    """
    session = pg["session"]()
    try:
        rows_updated = (
            session.query(ConversationLog)
            .filter(
                and_(
                    ConversationLog.message_id == message_id,
                    ConversationLog.org_id == org_id,
                )
            )
            .update(update_data)
        )
        _t = _time.time()
        session.commit()
        log_slow_call("PG commit update_conversation_log", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])
        return rows_updated > 0
    except Exception as e:
        session.rollback()
        logger.error(f"Error updating conversation log for message_id={message_id}: {str(e)}")
        return False
    finally:
        session.close()


async def add_bulk_user_entries(entries):
    session = pg["session"]()
    try:
        user_history = [user_bridge_config_history(**data) for data in entries]
        session.add_all(user_history)
        _t = _time.time()
        session.commit()
        log_slow_call("PG commit add_bulk_user_entries", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])
    except Exception as e:
        session.rollback()
        logger.error(f"Error in creating bulk user entries: {str(e)}")
    finally:
        session.close()
