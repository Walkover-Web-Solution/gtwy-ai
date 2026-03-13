from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from graph.edges import route_after_executor, route_after_planner, route_after_router
from graph.nodes.executor import executor_node
from graph.nodes.planner import planner_node
from graph.nodes.router import router_node
from graph.nodes.synthesizer import synthesizer_node
from graph.state import AgentState


def build_graph():
    """Builds and compiles the LangGraph agent with MemorySaver checkpointer."""
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("router", router_node)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("synthesizer", synthesizer_node)

    # Entry point
    graph.set_entry_point("router")

    # Router → planner (new plan) or executor (plan already approved)
    graph.add_conditional_edges("router", route_after_router, {
        "planner": "planner",
        "executor": "executor",
    })

    # Planner → question (interrupt) OR wait for approval OR execute (if already approved)
    graph.add_conditional_edges("planner", route_after_planner, {
        "wait_for_human": END,      # graph stops, WebSocket sends question, waits for answer
        "wait_for_approval": END,   # graph stops, WebSocket sends plan, waits for approval
        "executor": "executor",
    })

    # Executor → loop or synthesizer
    graph.add_conditional_edges("executor", route_after_executor, {
        "executor": "executor",
        "synthesizer": "synthesizer",
    })

    # Synthesizer → END
    graph.add_edge("synthesizer", END)

    # Compile with in-memory checkpointer
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled, checkpointer
