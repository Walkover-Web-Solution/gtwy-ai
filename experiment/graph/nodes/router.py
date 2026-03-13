from graph.state import AgentState


def router_node(state: AgentState) -> dict:
    """Entry node — no LLM call, just passes state through.
    Routing is handled by conditional edges based on state['mode']."""
    return {}
