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
    """After planning: if AI needs to ask a question → interrupt, else wait for plan approval."""
    if state.get("needs_question"):
        return "wait_for_human"
    if not state.get("plan_approved"):
        return "wait_for_approval"
    return "executor"


def route_after_executor(state: AgentState) -> str:
    """After executing a task:
    - If more tasks remain AND next step not yet approved → wait for user approval
    - If more tasks remain AND step already approved → execute next
    - If no more tasks → synthesizer
    """
    idx = state["current_task_index"]
    total = len(state["tasks"])

    if idx >= total:
        return "synthesizer"

    # More tasks remain — wait for per-step approval unless already granted
    if state.get("step_approved"):
        return "executor"
    return "wait_for_step_approval"
