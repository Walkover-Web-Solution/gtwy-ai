import os
import uuid

from db.agent_db_service import get_agent
from db.session_db_service import append_message, create_session, update_session
from graph.builder import build_agent_graph, build_default_graph
from graph.state import AgentState


async def invoke_agent(agent_id: str, goal: str, api_key: str = None, org_id: str = "default") -> dict:
    """Invoke an agent synchronously (non-streaming). Returns final answer."""
    agent_config = await get_agent(agent_id)
    if not agent_config:
        return {"error": f"Agent '{agent_id}' not found."}

    if agent_config.get("status") != "active":
        return {"error": f"Agent '{agent_id}' is not active."}

    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_api_key:
        return {"error": "No API key available."}

    # Build dynamic graph from agent config
    compiled_graph, checkpointer = await build_agent_graph(agent_config)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # Create session
    session = await create_session({
        "agent_id": agent_id,
        "org_id": org_id,
        "thread_id": thread_id,
        "goal": goal,
    })

    await append_message(session["session_id"], {"role": "user", "content": goal})

    initial_state = {
        "thread_id": thread_id,
        "goal": goal,
        "mode": "plan",
        "api_key": resolved_api_key,
        "agent_id": agent_id,
        "tasks": [],
        "completed_tasks": [],
        "current_task_index": 0,
        "final_answer": None,
        "needs_question": False,
        "question_text": None,
        "question_options": None,
        "human_input": None,
        "plan_approved": True,  # Auto-approve for REST invocation
        "step_approved": True,  # Auto-approve steps for REST invocation
        "step_feedback": None,
        "direct_messages": [],
        "built_steps": [],
        "scratchpad": [],
        "plan_revision_count": 0,
        "needs_replan": False,
        "replan_reason": None,
    }

    # Run the graph to completion
    final_state = await compiled_graph.ainvoke(initial_state, config)

    final_answer = final_state.get("final_answer", "No answer produced.")

    # Update session
    await append_message(session["session_id"], {"role": "assistant", "content": final_answer})
    await update_session(session["session_id"], {"status": "completed", "state": dict(final_state)})

    return {
        "agent_id": agent_id,
        "session_id": session["session_id"],
        "thread_id": thread_id,
        "goal": goal,
        "final_answer": final_answer,
        "tasks": final_state.get("tasks", []),
    }


async def get_compiled_graph_for_agent(agent_id: str = None):
    """Get a compiled graph for an agent. Falls back to default graph if no agent_id."""
    if not agent_id:
        return build_default_graph()

    agent_config = await get_agent(agent_id)
    if not agent_config:
        return build_default_graph()

    return await build_agent_graph(agent_config)
