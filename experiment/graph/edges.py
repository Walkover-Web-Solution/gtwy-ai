from graph.state import AgentState


def route_after_router(state: AgentState) -> str:
    """Routes based on user-selected mode: 'plan' or 'direct'."""
    if state["mode"] == "plan":
        return "planner"
    return "direct"


def route_after_planner(state: AgentState) -> str:
    """After planning: if AI needs to ask a question → interrupt, else → execute."""
    if state.get("needs_question"):
        return "wait_for_human"
    return "executor"


def route_after_executor(state: AgentState) -> str:
    """After executing a task: loop if more tasks, else → synthesizer."""
    if state["current_task_index"] < len(state["tasks"]):
        return "executor"
    return "synthesizer"
