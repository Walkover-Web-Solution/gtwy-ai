from datetime import datetime

from sqlalchemy import and_, text

from globals import logger
from models.index import combined_models as models
from models.postgres.pg_models import (
    ConversationLog,
    system_prompt_versionings,
    user_bridge_config_history,
)

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
        session.commit()
        return {"id": new_prompt.id}
    except Exception as error:
        session.rollback()
        logger.error(f"Error in storing system prompt: {str(error)}")
        raise error
    finally:
        session.close()


async def add_bulk_user_entries(entries):
    session = pg["session"]()
    try:
        user_history = [user_bridge_config_history(**data) for data in entries]
        session.add_all(user_history)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error in creating bulk user entries: {str(e)}")
    finally:
        session.close()
