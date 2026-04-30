import json
from datetime import datetime, timezone

from src.configs.constant import redis_keys
from src.services.cache_service import delete_in_cache, find_in_cache, store_in_cache
from src.services.todo.plan_checkpoint import CheckpointManager

PLAN_TTL = 172800  # 48 hours
_checkpoint_manager = CheckpointManager()


def _build_redis_key(org_id, bridge_id, thread_id, sub_thread_id):
    return f"{redis_keys['plan_']}{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}"


def _build_session_key(org_id, bridge_id, thread_id, sub_thread_id):
    """Build Redis key for planner session memory.

    Scoped per (thread_id, sub_thread_id) — same scope as the plan itself — so
    a new sub-thread starts with a clean Q&A history and does not leak context
    from unrelated sub-threads under the same thread.
    """
    return f"{redis_keys['plan_']}session_{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}"


async def save_plan(plan):
    org_id = plan["org_id"]
    bridge_id = plan["bridge_id"]
    thread_id = plan["thread_id"]
    sub_thread_id = plan["sub_thread_id"]

    now = datetime.now(timezone.utc).isoformat()
    plan["created_at"] = plan.get("created_at") or now
    plan["updated_at"] = now

    redis_key = _build_redis_key(org_id, bridge_id, thread_id, sub_thread_id)
    await store_in_cache(redis_key, plan, ttl=PLAN_TTL)

    try:
        await _sync_pending_questions_to_session(plan)
    except Exception:
        pass
    
    try:
        old_checkpoint = await get_latest_checkpoint(org_id, bridge_id, thread_id, sub_thread_id)
        interaction_type = "initial_plan" if old_checkpoint is None else "execution_update"
        
        checkpoint = _checkpoint_manager.create_checkpoint(
            plan=plan,
            interaction_type=interaction_type,
            old_checkpoint=old_checkpoint
        )
        
        await add_checkpoint(org_id, bridge_id, thread_id, sub_thread_id, checkpoint)
    except Exception as e:
        from globals import logger
        logger.error(f"Failed to create checkpoint: {e}", exc_info=True)


async def get_plan(org_id, bridge_id, thread_id, sub_thread_id):
    redis_key = _build_redis_key(org_id, bridge_id, thread_id, sub_thread_id)
    cached = await find_in_cache(redis_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


async def update_plan(plan):
    plan["updated_at"] = datetime.now(timezone.utc).isoformat()
    await save_plan(plan)


async def update_task_status(org_id, bridge_id, thread_id, sub_thread_id, task_id, status, result=None, error=None):
    plan = await get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return None

    task = plan.get("tasks", {}).get(task_id)
    if not task:
        return None

    task["status"] = status
    if result is not None:
        task["result"] = result
    if error is not None:
        task["error"] = error

    await update_plan(plan)
    return plan


async def delete_plan(org_id, bridge_id, thread_id, sub_thread_id):
    redis_key = _build_redis_key(org_id, bridge_id, thread_id, sub_thread_id)
    await delete_in_cache(redis_key)


async def get_planner_session(org_id, bridge_id, thread_id, sub_thread_id):
    """Get planner session memory containing Q&A history, scoped to
    (thread_id, sub_thread_id) just like the plan."""
    redis_key = _build_session_key(org_id, bridge_id, thread_id, sub_thread_id)
    cached = await find_in_cache(redis_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "org_id": org_id,
        "bridge_id": bridge_id,
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "qa_history": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


_MAX_QA_HISTORY_STORED = 25  # hard cap on persisted Q&A pairs per session


async def _persist_session(session):
    """Persist a session dict to cache with TTL. Caps qa_history length."""
    qa_history = session.get("qa_history") or []
    if len(qa_history) > _MAX_QA_HISTORY_STORED:
        qa_history = qa_history[-_MAX_QA_HISTORY_STORED:]
    session["qa_history"] = qa_history
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    redis_key = _build_session_key(
        session["org_id"], session["bridge_id"], session["thread_id"], session["sub_thread_id"]
    )
    await store_in_cache(redis_key, session, ttl=PLAN_TTL)
    return session


async def add_to_planner_session(org_id, bridge_id, thread_id, sub_thread_id, question, answer):
    session = await get_planner_session(org_id, bridge_id, thread_id, sub_thread_id)
    qa_history = session.get("qa_history") or []
    now = datetime.now(timezone.utc).isoformat()

    # Walk backwards so the most recent matching pending question wins.
    matched = False
    for entry in reversed(qa_history):
        if entry.get("question") == question and entry.get("answer") in (None, ""):
            entry["answer"] = answer
            entry["answered_at"] = now
            matched = True
            break
    if not matched:
        qa_history.append({
            "question": question,
            "answer": answer,
            "timestamp": now,
            "answered_at": now,
        })

    session["qa_history"] = qa_history
    return await _persist_session(session)


async def _sync_pending_questions_to_session(plan):
    """Mirror any `waiting_for_user` questions in the plan into session memory.

    Adds an entry `{question, answer: None}` for every waiting question that
    hasn't been recorded yet. Answered questions (entries with non-null
    `answer` in history, or tasks with a non-null `human_response` matching
    the question) are left untouched.
    """
    tasks = (plan or {}).get("tasks") or {}
    pending_questions = [
        t.get("human_query")
        for t in tasks.values()
        if t.get("status") == "waiting_for_user"
        and t.get("human_query")
        and not t.get("human_response")
    ]
    if not pending_questions:
        return

    org_id = plan["org_id"]
    bridge_id = plan["bridge_id"]
    thread_id = plan["thread_id"]
    sub_thread_id = plan["sub_thread_id"]

    session = await get_planner_session(org_id, bridge_id, thread_id, sub_thread_id)
    qa_history = session.get("qa_history") or []
    known_questions = {e.get("question") for e in qa_history if e.get("question")}

    now = datetime.now(timezone.utc).isoformat()
    appended = False
    for q in pending_questions:
        if q in known_questions:
            continue
        qa_history.append({
            "question": q,
            "answer": None,
            "timestamp": now,
        })
        known_questions.add(q)
        appended = True

    if appended:
        session["qa_history"] = qa_history
        await _persist_session(session)


async def clear_planner_session(org_id, bridge_id, thread_id, sub_thread_id):
    """Clear planner session memory for the given (thread, sub_thread) scope."""
    redis_key = _build_session_key(org_id, bridge_id, thread_id, sub_thread_id)
    await delete_in_cache(redis_key)


_MAX_CHECKPOINTS_STORED = 10


def _build_checkpoint_key(org_id, bridge_id, thread_id, sub_thread_id):
    """Build Redis key for plan checkpoints."""
    return f"{redis_keys['plan_']}checkpoint_{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}"


async def add_checkpoint(org_id, bridge_id, thread_id, sub_thread_id, checkpoint):
    """
    Add a new checkpoint to the checkpoint history.
    
    Maintains a circular buffer of the last N checkpoints.
    
    Args:
        org_id: Organization ID
        bridge_id: Bridge ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        checkpoint: Checkpoint dictionary to add
    """
    redis_key = _build_checkpoint_key(org_id, bridge_id, thread_id, sub_thread_id)
    
    cached = await find_in_cache(redis_key)
    if cached:
        try:
            checkpoint_data = json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            checkpoint_data = {"checkpoints": []}
    else:
        checkpoint_data = {"checkpoints": []}
    
    checkpoints = checkpoint_data.get("checkpoints", [])
    
    version = len(checkpoints) + 1
    checkpoint["version"] = version
    
    checkpoints.append(checkpoint)
    
    if len(checkpoints) > _MAX_CHECKPOINTS_STORED:
        checkpoints = checkpoints[-_MAX_CHECKPOINTS_STORED:]
    
    checkpoint_data["checkpoints"] = checkpoints
    checkpoint_data["current_version"] = version
    
    await store_in_cache(redis_key, checkpoint_data, ttl=PLAN_TTL)


async def get_latest_checkpoint(org_id, bridge_id, thread_id, sub_thread_id):
    """
    Get the latest checkpoint for a plan.
    
    Args:
        org_id: Organization ID
        bridge_id: Bridge ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        
    Returns:
        Latest checkpoint dictionary or None if no checkpoints exist
    """
    redis_key = _build_checkpoint_key(org_id, bridge_id, thread_id, sub_thread_id)
    cached = await find_in_cache(redis_key)
    
    if cached:
        try:
            checkpoint_data = json.loads(cached)
            checkpoints = checkpoint_data.get("checkpoints", [])
            if checkpoints:
                return checkpoints[-1]
        except (json.JSONDecodeError, TypeError):
            pass
    
    return None


async def get_checkpoint_history(org_id, bridge_id, thread_id, sub_thread_id, limit=10):
    """
    Get checkpoint history for a plan.
    
    Args:
        org_id: Organization ID
        bridge_id: Bridge ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        limit: Maximum number of checkpoints to return
        
    Returns:
        List of checkpoint dictionaries (most recent first)
    """
    redis_key = _build_checkpoint_key(org_id, bridge_id, thread_id, sub_thread_id)
    cached = await find_in_cache(redis_key)
    
    if cached:
        try:
            checkpoint_data = json.loads(cached)
            checkpoints = checkpoint_data.get("checkpoints", [])
            return checkpoints[-limit:] if checkpoints else []
        except (json.JSONDecodeError, TypeError):
            pass
    
    return []


async def get_task(org_id, bridge_id, thread_id, sub_thread_id, task_id):
    """
    Get a single task from the plan without loading the full plan.
    
    Args:
        org_id: Organization ID
        bridge_id: Bridge ID
        thread_id: Thread ID
        sub_thread_id: Sub-thread ID
        task_id: Task ID to retrieve
        
    Returns:
        Task dictionary or None if not found
    """
    plan = await get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if plan:
        return plan.get("tasks", {}).get(task_id)
    return None


async def clear_checkpoints(org_id, bridge_id, thread_id, sub_thread_id):
    """Clear all checkpoints for the given plan scope."""
    redis_key = _build_checkpoint_key(org_id, bridge_id, thread_id, sub_thread_id)
    await delete_in_cache(redis_key)
