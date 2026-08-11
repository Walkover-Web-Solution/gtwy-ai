import json
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
from src.configs.constant import redis_keys
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


def _find_examples_in_rounds(rounds, remaining_names, found):
    """Single pass over an iterable of round-dicts ({call_id: {name, args,
    data}}) — same shape whether it came from Postgres
    conversation_logs.tools_call_data or a Redis-cached conversation entry's
    tools_call_data — matching against ALL of `remaining_names` at once
    instead of one tool at a time. Mutates `found` (dict: name -> {args,
    response}) and `remaining_names` (set, shrinks as matches are found) in
    place. Caller controls recency by how it orders `rounds` (reversed() for
    newest-first) and stops once `remaining_names` is empty.
    """
    for round_data in rounds:
        if not remaining_names:
            return
        if not isinstance(round_data, dict):
            continue
        for call in round_data.values():
            if not isinstance(call, dict):
                continue
            call_name = call.get("name")
            if call_name in remaining_names:
                data = call.get("data")
                response = data.get("response") if isinstance(data, dict) else data
                if response is not None:
                    found[call_name] = {"args": call.get("args"), "response": response}
                    remaining_names.discard(call_name)


async def get_recent_tool_examples(org_id, bridge_id, thread_id, sub_thread_id, tool_names, version_id="", scan_limit=20):
    """Return the most recent real {args, response} pair recorded for each
    name in `tool_names`, in THIS thread/sub_thread — as {name: {args,
    response}}, omitting names that have never been called there.

    Used by src/services/auto_exec/prompt_builder.py so the AI can see a
    real response shape (not just the request schema) when writing code
    that chains one tool's result into another's arguments. Scoped to
    thread_id + sub_thread_id (not bridge-wide) so examples never leak in
    from unrelated conversations.

    Batched across all of `tool_names` in one fetch instead of one lookup
    per tool — fetches the conversation cache (and, only if still needed,
    Postgres) exactly ONCE per call, then scans that single dataset for
    every requested name in a single pass. Checks TWO places in order,
    cheapest/freshest first:
    1. The conversation cache Redis key (cd_conversation_{version_id}_{thread_id}_
       {sub_thread_id}) that src/services/utils/common_utils.py::save_conversations_to_redis
       already writes to on every turn — no extra cache key, already thread-scoped,
       already populated at zero extra cost.
    2. A Postgres scan of conversation_logs (last `scan_limit` rows for this
       thread) as the fallback, used for whichever names the conversation
       cache didn't cover (it's a shallow rolling window of ~9 entries and
       can miss a tool called earlier in a long thread).
    """
    from src.services.cache_service import find_in_cache

    remaining = {name for name in tool_names if name}
    found = {}
    if not remaining:
        return found

    conversation_cache_key = f"{redis_keys['conversation_']}{version_id}_{thread_id}_{sub_thread_id}"
    cached_conversation = await find_in_cache(conversation_cache_key)
    if cached_conversation:
        try:
            conversation_entries = json.loads(cached_conversation) or []
        except (json.JSONDecodeError, TypeError):
            conversation_entries = []
        for entry in reversed(conversation_entries):
            if not remaining:
                break
            entry_tools_call_data = entry.get("tools_call_data") if isinstance(entry, dict) else None
            if not entry_tools_call_data:
                continue
            _find_examples_in_rounds(reversed(entry_tools_call_data), remaining, found)

    if not remaining:
        return found

    session = pg["session"]()
    try:
        _t = _time.time()
        logs = (
            session.query(ConversationLog)
            .filter(
                and_(
                    ConversationLog.org_id == org_id,
                    ConversationLog.bridge_id == bridge_id,
                    ConversationLog.thread_id == thread_id,
                    ConversationLog.sub_thread_id == sub_thread_id,
                    ConversationLog.tools_call_data.isnot(None),
                )
            )
            .order_by(ConversationLog.created_at.desc())
            .limit(scan_limit)
            .all()
        )
        log_slow_call("PG query get_recent_tool_examples", _time.time() - _t, SLOW_CALL_THRESHOLDS["pg"])

        for log in logs:
            if not remaining:
                break
            _find_examples_in_rounds(reversed(log.tools_call_data or []), remaining, found)

        return found
    except Exception as e:
        logger.error(f"Error in get_recent_tool_examples: {str(e)}")
        return found
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
