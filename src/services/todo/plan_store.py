import json
from datetime import datetime, timezone

from src.configs.constant import redis_keys
from src.services.cache_service import delete_in_cache, find_in_cache, store_in_cache

PLAN_TTL = 172800  # 48 hours


def _build_redis_key(org_id, bridge_id, thread_id, sub_thread_id):
    return f"{redis_keys['plan_']}{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}"


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
