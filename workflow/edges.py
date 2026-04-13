from workflow.state import WorkflowState


def route_after_planner(state: WorkflowState) -> str:
    if state.get("planner_response") and state.get("worker_question_task_id"):
        return "executor"
    if state.get("needs_question"):
        return "wait_for_human"
    require_plan_approval = state.get("user_config", {}).get("require_plan_approval", True)
    if require_plan_approval and not state.get("plan_approved"):
        return "wait_for_approval"
    return "executor"


def route_after_human_input(state: WorkflowState) -> str:
    if state.get("planner_response") and state.get("worker_question_task_id"):
        return "executor"
    return "planner"


def route_after_plan_approval(state: WorkflowState) -> str:
    if state.get("plan_approved"):
        if state.get("user_config", {}).get("require_step_approval", False):
            return "wait_for_step_approval"
        return "executor"
    return "planner"


def _has_pending_tasks(state: WorkflowState) -> bool:
    tasks = state.get("tasks", [])
    completed_ids = {task["id"] for task in tasks if task["status"] in ("completed", "skipped")}
    for task in tasks:
        if task["status"] != "pending":
            continue
        deps = task.get("depends_on", [])
        if all(dep_id in completed_ids for dep_id in deps):
            return True
    return False


def route_after_executor(state: WorkflowState) -> str:
    if state.get("needs_worker_clarification"):
        return "planner"
    if state.get("needs_replan"):
        return "planner"
    if _has_pending_tasks(state):
        require_step_approval = state.get("user_config", {}).get("require_step_approval", False)
        if not require_step_approval:
            return "executor"
        if state.get("step_approved"):
            return "executor"
        return "wait_for_step_approval"
    return "synthesizer"


def route_after_step_approval(state: WorkflowState) -> str:
    if state.get("step_approved"):
        return "executor"
    return "planner"
