from src.services.todo.executor.constants import TERMINAL_STATUSES, INTERRUPTED_TASK_RECOVERY_NOTE


def get_tasks(plan: dict) -> dict:
    """Return tasks as a dict keyed by task id.

    The LLM stores tasks as a list under plan['plan']['tasks']; this normalises
    both list and dict shapes so the rest of the executor can work uniformly.
    """
    raw = (plan.get("plan") or {}).get("tasks") or []
    if isinstance(raw, list):
        return {t["id"]: t for t in raw if isinstance(t, dict) and t.get("id")}
    if isinstance(raw, dict):
        return raw
    return {}


def set_tasks(plan: dict, tasks_dict: dict) -> None:
    """Write tasks dict back into plan['plan']['tasks'] as a list."""
    if "plan" not in plan or plan["plan"] is None:
        plan["plan"] = {}
    plan["plan"]["tasks"] = list(tasks_dict.values())


def get_runnable_tasks(tasks: dict) -> list:
    """Return task IDs that are pending and have all dependencies completed."""
    return [
        task_id
        for task_id, task in tasks.items()
        if task["status"] == "pending"
        and all(
            tasks.get(dep, {}).get("status") == "completed"
            for dep in task.get("dependencies", [])
        )
    ]


def is_plan_complete(tasks: dict) -> bool:
    """True when every task is in a terminal state."""
    return all(t["status"] in TERMINAL_STATUSES for t in tasks.values())


def is_plan_blocked(tasks: dict) -> bool:
    """True when the plan cannot make further progress without external input.

    Blocking conditions (any one is sufficient):
      - A task is waiting_for_user.
      - Nothing is running and no pending task has all its deps met.
    """
    if any(t["status"] == "waiting_for_user" for t in tasks.values()):
        return True

    if any(t["status"] == "in_progress" for t in tasks.values()):
        return False

    for task in tasks.values():
        if task["status"] == "pending" and all(
            tasks.get(dep, {}).get("status") == "completed"
            for dep in task.get("dependencies", [])
        ):
            return False

    return any(t["status"] == "pending" for t in tasks.values())


def recover_interrupted_tasks(plan: dict) -> list:
    """Re-queue any tasks left in_progress from a previous interrupted run.

    Returns the list of recovered task IDs.
    """
    tasks = get_tasks(plan or {})
    recovered = []
    for task_id, task in tasks.items():
        if task.get("status") != "in_progress":
            continue
        task["status"] = "pending"
        task["is_error"] = False
        task["error"] = None
        task["result"] = None
        task["last_recovery_reason"] = INTERRUPTED_TASK_RECOVERY_NOTE
        recovered.append(task_id)
    return recovered
