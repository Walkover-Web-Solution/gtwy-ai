from graph.state import AgentState


def route_after_router(state: AgentState) -> str:
    """If plan already approved skip replanning, go straight to executor."""
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
    """After executing a task: loop if more tasks, else → synthesizer."""
    if state["current_task_index"] < len(state["tasks"]):
        return "executor"
    return "synthesizer"
