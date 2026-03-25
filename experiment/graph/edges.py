from graph.state import AgentState


def route_after_router(state: AgentState) -> str:
    """Route based on mode and state:
    - direct mode → direct node (single LLM call, no planner)
    - plan already approved → executor
    - default → planner
    """
    if state.get("mode") == "direct":
        return "direct"
    if state.get("plan_approved"):
        return "executor"
    return "planner"


def route_after_planner(state: AgentState) -> str:
    """After planning:
    - If planner answered a worker question → back to executor
    - If AI needs to ask the user something → interrupt
    - If plan not approved → wait for approval
    - Otherwise → executor
    """
    # Planner answered a worker's clarification → resume executor
    if state.get("planner_response") and state.get("worker_question_task_id"):
        return "executor"
    if state.get("needs_question"):
        return "wait_for_human"
    if not state.get("plan_approved"):
        return "wait_for_approval"
    return "executor"


def _has_pending_tasks(state: AgentState) -> bool:
    """Check if any tasks are still pending (dependency-aware)."""
    tasks = state.get("tasks", [])
    completed_ids = {t["id"] for t in tasks if t["status"] in ("completed", "skipped")}
    for t in tasks:
        if t["status"] == "pending":
            deps = t.get("depends_on", [])
            if all(dep_id in completed_ids for dep_id in deps):
                return True
    return False


def route_after_executor(state: AgentState) -> str:
    """After executing a batch of tasks:
    - If worker needs clarification from planner → planner
    - If a task failed and needs re-plan → planner (re-plan path)
    - If more runnable tasks remain AND step already approved → executor
    - If more runnable tasks remain AND need approval → wait for step approval
    - If no more tasks → synthesizer
    """
    # Worker asked planner a question → route to planner for clarification
    if state.get("needs_worker_clarification"):
        return "planner"

    # Re-plan path: executor flagged a failure
    if state.get("needs_replan"):
        return "planner"

    # Check for remaining runnable tasks
    if _has_pending_tasks(state):
        if state.get("step_approved"):
            return "executor"
        return "wait_for_step_approval"

    return "synthesizer"
