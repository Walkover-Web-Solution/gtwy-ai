from langgraph.graph import END, StateGraph

from workflow.edges import (
    route_after_executor,
    route_after_human_input,
    route_after_plan_approval,
    route_after_planner,
    route_after_step_approval,
)
from workflow.nodes import (
    make_executor_node,
    make_human_input_node,
    make_plan_approval_node,
    make_planner_node,
    make_step_approval_node,
    make_synthesizer_node,
)
from workflow.state import WorkflowState


def build_workflow_graph(research_tools, execution_tools, tool_schemas, checkpointer):
    graph = StateGraph(WorkflowState)

    graph.add_node("planner", make_planner_node(research_tools, tool_schemas))
    graph.add_node("wait_for_human", make_human_input_node())
    graph.add_node("wait_for_approval", make_plan_approval_node())
    graph.add_node("wait_for_step_approval", make_step_approval_node())
    graph.add_node("executor", make_executor_node(execution_tools))
    graph.add_node("synthesizer", make_synthesizer_node())

    graph.set_entry_point("planner")
    graph.add_conditional_edges("planner", route_after_planner, {
        "wait_for_human": "wait_for_human",
        "wait_for_approval": "wait_for_approval",
        "executor": "executor",
    })
    graph.add_conditional_edges("wait_for_human", route_after_human_input, {
        "planner": "planner",
        "executor": "executor",
    })
    graph.add_conditional_edges("wait_for_approval", route_after_plan_approval, {
        "planner": "planner",
        "wait_for_step_approval": "wait_for_step_approval",
        "executor": "executor",
    })
    graph.add_conditional_edges("executor", route_after_executor, {
        "executor": "executor",
        "wait_for_step_approval": "wait_for_step_approval",
        "planner": "planner",
        "synthesizer": "synthesizer",
    })
    graph.add_conditional_edges("wait_for_step_approval", route_after_step_approval, {
        "planner": "planner",
        "executor": "executor",
    })
    graph.add_edge("synthesizer", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled
