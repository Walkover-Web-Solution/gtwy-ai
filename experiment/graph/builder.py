from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from graph.edges import route_after_executor, route_after_planner, route_after_router
from graph.nodes.direct import direct_node, make_direct_node
from graph.nodes.executor import executor_node, make_executor_node
from graph.nodes.planner import make_planner_node, planner_node
from graph.nodes.router import router_node
from graph.nodes.synthesizer import make_synthesizer_node, synthesizer_node
from graph.state import AgentState


def _build_graph_skeleton(planner_fn, executor_fn, synthesizer_fn, direct_fn=None):
    """Shared graph topology used by both default and dynamic builders."""
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("router", router_node)
    graph.add_node("planner", planner_fn)
    graph.add_node("executor", executor_fn)
    graph.add_node("synthesizer", synthesizer_fn)
    graph.add_node("direct", direct_fn or direct_node)

    # Entry point
    graph.set_entry_point("router")

    # Router → direct (single-call mode) or planner (new plan) or executor (plan already approved)
    graph.add_conditional_edges("router", route_after_router, {
        "direct": "direct",
        "planner": "planner",
        "executor": "executor",
    })

    # Direct → END (no further processing needed)
    graph.add_edge("direct", END)

    # Planner → question (interrupt) OR wait for approval OR execute (if already approved)
    graph.add_conditional_edges("planner", route_after_planner, {
        "wait_for_human": END,      # graph stops, WebSocket sends question, waits for answer
        "wait_for_approval": END,   # graph stops, WebSocket sends plan, waits for approval
        "executor": "executor",
    })

    # Executor → loop, wait for per-step approval, re-plan on failure, or synthesizer
    graph.add_conditional_edges("executor", route_after_executor, {
        "executor": "executor",
        "wait_for_step_approval": END,   # graph stops, WebSocket sends step proposal, waits for user
        "planner": "planner",            # re-plan path when a task fails
        "synthesizer": "synthesizer",
    })

    # Synthesizer → END
    graph.add_edge("synthesizer", END)

    # Compile with in-memory checkpointer
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled, checkpointer


def build_graph():
    """Builds the default graph with hardcoded nodes (backward compatible)."""
    return _build_graph_skeleton(planner_node, executor_node, synthesizer_node)


# Alias for clarity
build_default_graph = build_graph


async def build_agent_graph(agent_config: dict):
    """Builds a dynamic graph from an agent's DB configuration.

    Loads tools from DB, creates A2A tools for sub-agents, and builds
    planner/executor/synthesizer nodes parameterized by the agent config.

    Returns (compiled_graph, checkpointer, tool_schemas, resolved_agent_config).
    """
    from db.agent_db_service import get_agent
    from services.a2a_service import create_a2a_tool
    from services.tool_registry import execute_pretool, extract_tool_schemas, load_tools_for_agent

    agent_id = agent_config["agent_id"]

    # 1. Load tools from DB
    tools = await load_tools_for_agent(agent_id)

    # 2. Add A2A tools for each sub-agent
    for sub_agent_id in agent_config.get("sub_agents", []):
        sub_agent = await get_agent(sub_agent_id)
        if sub_agent and sub_agent.get("status") == "active":
            a2a_tool = create_a2a_tool(sub_agent_id, sub_agent)
            tools.append(a2a_tool)

    # 3. Run pretool if configured — execute the tool and replace {{pretool}} in system_prompt
    pretool_id = agent_config.get("pretool")
    if pretool_id and "{{pretool}}" in (agent_config.get("system_prompt") or ""):
        pretool_input = agent_config.get("pretool_input", {})
        pretool_output = await execute_pretool(pretool_id, pretool_input)
        agent_config = {
            **agent_config,
            "system_prompt": agent_config["system_prompt"].replace("{{pretool}}", pretool_output),
        }

    # 4. Extract tool schemas for planner visibility
    tool_schemas = extract_tool_schemas(tools)

    # 5. Build parameterized node functions
    dynamic_planner = make_planner_node(agent_config, tools=tools)
    dynamic_executor = make_executor_node(agent_config, tools)
    dynamic_synthesizer = make_synthesizer_node(agent_config)
    dynamic_direct = make_direct_node(agent_config, tools)

    compiled, checkpointer = _build_graph_skeleton(dynamic_planner, dynamic_executor, dynamic_synthesizer, dynamic_direct)
    return compiled, checkpointer, tool_schemas, agent_config
