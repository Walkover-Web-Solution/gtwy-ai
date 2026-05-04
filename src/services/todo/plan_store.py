import json
from datetime import datetime, timezone

from src.configs.constant import redis_keys
from src.services.cache_service import delete_in_cache, find_in_cache, store_in_cache

PLAN_TTL = 172800  # 48 hours


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

    # Mirror any new `waiting_for_user` questions into session memory so the
    # planner sees them as pending even before the user replies. Failures here
    # must not break plan persistence — they are a best-effort sync.
    try:
        await _sync_pending_questions_to_session(plan)
    except Exception:
        pass


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


def _get_tasks(plan):
    raw = (plan.get("plan") or {}).get("tasks") or []
    if isinstance(raw, list):
        return {t["id"]: t for t in raw if isinstance(t, dict) and t.get("id")}
    if isinstance(raw, dict):
        return raw
    return {}


def _set_tasks(plan, tasks_dict):
    if "plan" not in plan or plan["plan"] is None:
        plan["plan"] = {}
    plan["plan"]["tasks"] = list(tasks_dict.values())


async def update_task_status(org_id, bridge_id, thread_id, sub_thread_id, task_id, status, result=None, error=None):
    plan = await get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return None

    tasks = _get_tasks(plan)
    task = tasks.get(task_id)
    if not task:
        return None

    task["status"] = status
    if result is not None:
        task["result"] = result
    if error is not None:
        task["error"] = error

    _set_tasks(plan, tasks)
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
    tasks = _get_tasks(plan or {})
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
