from globals import logger
from src.services.todo import plan_store
from src.services.todo.executor.plan_tasks import get_tasks, set_tasks


async def resume_task(
    org_id: str,
    bridge_id: str,
    thread_id: str,
    sub_thread_id: str,
    task_id: str,
    human_response: str,
) -> dict:
    """Store human response and reset the task to pending.

    The caller is responsible for driving execute_plan so that execution
    events are streamed back to the client.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return {"success": False, "error": "Plan not found"}

    tasks = get_tasks(plan)
    task = tasks.get(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}
    if task["status"] != "waiting_for_user":
        return {
            "success": False,
            "error": f"Task {task_id} is not waiting for user input (status: {task['status']})",
        }

    logger.info(f"[RESPOND] Task {task_id}: waiting_for_user -> pending")
    task["human_response"] = human_response
    task["status"] = "pending"
    task["retry"] = 0
    set_tasks(plan, tasks)
    await plan_store.update_plan(plan)

    for q in task.get("questions") or []:
        question_text = q.get("question") if isinstance(q, dict) else q
        if question_text:
            await plan_store.add_to_planner_session(
                org_id, bridge_id, thread_id, sub_thread_id, question_text, human_response
            )

    return {"success": True, "plan": plan}
