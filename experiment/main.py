import json
import os
import sys
import traceback
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Add experiment dir to path so graph package is importable
sys.path.insert(0, os.path.dirname(__file__))

from db.connection import close_db, init_db
from graph.builder import build_graph
from routes.a2a_routes import router as a2a_router
from routes.agent_routes import router as agent_router
from routes.tool_routes import router as tool_router
from services.agent_service import get_compiled_graph_for_agent

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events."""
    await init_db()
    print("Experiment server started. JSON file store ready.")
    yield
    await close_db()
    print("Experiment server stopped.")


app = FastAPI(title="Experiment: Agentic AI Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register REST API routers
app.include_router(agent_router, prefix="/api")
app.include_router(tool_router, prefix="/api")
app.include_router(a2a_router, prefix="/api")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Build default graph once at startup (backward compatible)
default_compiled_graph, default_checkpointer = build_graph()


async def ws_send(ws: WebSocket, event: str, data: dict):
    """Send a JSON event over WebSocket."""
    await ws.send_text(json.dumps({"event": event, "data": data}))


async def run_graph_and_stream(ws: WebSocket, state: dict, config: dict, compiled_graph=None, pending_state_out: list | None = None):
    """Run the graph and stream events to the WebSocket client."""
    graph = compiled_graph or default_compiled_graph

    # Stream events from the graph
    current_node = None
    current_task_id = None
    running_task_ids = []  # all task IDs in current executor batch
    streamed_text = ""

    async for event in graph.astream_events(state, config, version="v2"):
        kind = event.get("event", "")
        name = event.get("name", "")

        # Track which node we're in
        if kind == "on_chain_start" and name in ("planner", "executor", "synthesizer", "direct"):
            current_node = name

            if name == "planner":
                await ws_send(ws, "status", {"message": "Creating plan..."})
            elif name == "direct":
                await ws_send(ws, "status", {"message": "Thinking..."})
            elif name == "executor":
                try:
                    node_input = event.get("data", {}).get("input", {})
                    tasks = node_input.get("tasks", [])
                    # Find ALL runnable tasks in this batch (mirrors executor logic)
                    completed_ids = {t["id"] for t in tasks if t["status"] in ("completed", "skipped")}
                    running_task_ids = []
                    for t in tasks:
                        if t["status"] == "pending":
                            deps = t.get("depends_on", [])
                            if all(dep_id in completed_ids for dep_id in deps):
                                running_task_ids.append(t["id"])
                                await ws_send(ws, "task_start", {
                                    "task_id": t["id"],
                                    "title": t["title"],
                                })
                    current_task_id = running_task_ids[0] if running_task_ids else None
                    if len(running_task_ids) > 1:
                        await ws_send(ws, "status", {"message": f"Executing {len(running_task_ids)} tasks in parallel..."})
                    elif len(running_task_ids) == 1:
                        title = next((t["title"] for t in tasks if t["id"] == running_task_ids[0]), "")
                        await ws_send(ws, "status", {"message": f"Executing: {title}"})
                except Exception:
                    pass
                streamed_text = ""
            elif name == "synthesizer":
                await ws_send(ws, "status", {"message": "Preparing final output..."})
                streamed_text = ""

        # Stream LLM token chunks
        if kind == "on_chat_model_stream":
            content = ""
            try:
                chunk = event["data"]["chunk"]
                if hasattr(chunk, "content"):
                    content = chunk.content or ""
                if current_node == "executor" and current_task_id:
                    if hasattr(chunk, "tool_call_chunks") and chunk.tool_call_chunks:
                        tool_name = chunk.tool_call_chunks[0].get("name", "")
                        if tool_name:
                            await ws_send(ws, "tool_call", {"task_id": current_task_id, "tool": tool_name})
            except Exception:
                content = ""

            if content:
                streamed_text += content
                if current_node == "executor" and current_task_id:
                    await ws_send(ws, "task_chunk", {"task_id": current_task_id, "chunk": content})
                elif current_node == "synthesizer":
                    await ws_send(ws, "final_chunk", {"chunk": content})

        # Node completed
        if kind == "on_chain_end" and name in ("planner", "executor", "synthesizer", "direct"):
            if name == "planner":
                try:
                    planner_output = event.get("data", {}).get("output", {})
                    # Emit planner thinking (research + reasoning) to frontend
                    thinking = planner_output.get("planner_thinking", [])
                    if thinking:
                        await ws_send(ws, "planner_thinking", {"steps": thinking})

                    # Planner answered a worker's question → emit event, graph routes back to executor
                    if planner_output.get("planner_response") and not planner_output.get("needs_question"):
                        await ws_send(ws, "planner_response", {
                            "task_id": state.get("worker_question_task_id", ""),
                            "question": state.get("worker_question", ""),
                            "answer": planner_output["planner_response"],
                        })
                        # Don't return — graph continues to executor automatically

                    elif planner_output.get("needs_question"):
                        # Could be a normal question OR planner escalating a worker question to user
                        worker_ctx = state.get("worker_question")
                        await ws_send(ws, "question", {
                            "text": planner_output.get("question_text", ""),
                            "options": planner_output.get("question_options", []),
                            "worker_context": worker_ctx,  # so frontend knows this came from a worker doubt
                        })
                        return "waiting_for_answer"
                    else:
                        tasks = planner_output.get("tasks", [])
                        if tasks:
                            await ws_send(ws, "plan_ready", {
                                "tasks": [
                                    {
                                        "id": t["id"],
                                        "title": t["title"],
                                        "description": t["description"],
                                        "tool_name": t.get("tool_name"),
                                        "status": t["status"],
                                        "depends_on": t.get("depends_on", []),
                                        "priority": t.get("priority", "medium"),
                                        "acceptance_criteria": t.get("acceptance_criteria", ""),
                                        "estimated_complexity": t.get("estimated_complexity", "moderate"),
                                    }
                                    for t in tasks
                                ],
                                "is_replan": planner_output.get("plan_revision_count", 0) > 0,
                            })
                            if pending_state_out is not None:
                                pending_state_out.append({
                                    **state,
                                    "tasks": tasks,
                                    "needs_question": False,
                                    "plan_approved": False,
                                    "current_task_index": 0,
                                    "completed_tasks": state.get("completed_tasks", []),
                                    "scratchpad": state.get("scratchpad", []),
                                    "planner_thinking": thinking,
                                    "plan_revision_count": planner_output.get("plan_revision_count", 0),
                                    "needs_replan": False,
                                    "replan_reason": None,
                                    "human_input": planner_output.get("human_input", state.get("human_input")),
                                })
                            return "waiting_for_approval"
                except Exception:
                    pass

            elif name == "direct":
                try:
                    direct_output = event.get("data", {}).get("output", {})
                    import json as _json
                    response_objects = []
                    try:
                        parsed = _json.loads(direct_output.get("final_answer", ""))
                        response_objects = parsed.get("response", [])
                    except Exception:
                        txt = direct_output.get("final_answer", "")
                        if txt:
                            response_objects = [{"type": "text", "text": txt}]

                    needs_next = direct_output.get("needs_question") and direct_output.get("question_text") == "__next_step__"

                    await ws_send(ws, "step_built", {
                        "objects": response_objects,
                        "needs_next": needs_next,
                        "built_steps": direct_output.get("built_steps", []),
                    })

                    if needs_next:
                        snap_state = {**state, **direct_output, "human_input": None}
                        if pending_state_out is not None:
                            pending_state_out.append(snap_state)
                        return "waiting_for_next"
                    else:
                        await ws_send(ws, "done", {"final_answer": direct_output.get("final_answer", "")})
                        return "completed"
                except Exception:
                    pass

            elif name == "executor":
                try:
                    executor_output = event.get("data", {}).get("output", {})
                    snap_state = {**state, **executor_output}
                    output_tasks = executor_output.get("tasks", [])

                    # Emit task_done / task_failed for ALL tasks that completed in this batch
                    for t in output_tasks:
                        if t["id"] in running_task_ids:
                            if t["status"] == "completed":
                                await ws_send(ws, "task_done", {"task_id": t["id"]})
                            elif t["status"] == "failed":
                                await ws_send(ws, "task_failed", {
                                    "task_id": t["id"],
                                    "error": t.get("result", "Task failed"),
                                })

                    running_task_ids = []
                    current_task_id = None

                    # Send reflection data if available
                    for t in output_tasks:
                        if t.get("reflection"):
                            await ws_send(ws, "task_reflection", {
                                "task_id": t["id"],
                                "reflection": t["reflection"],
                            })

                    # Worker asked planner for clarification
                    if snap_state.get("needs_worker_clarification"):
                        await ws_send(ws, "worker_clarification", {
                            "task_id": snap_state.get("worker_question_task_id", ""),
                            "question": snap_state.get("worker_question", ""),
                        })
                        if pending_state_out is not None:
                            pending_state_out.append(snap_state)
                        # Don't return — graph continues to planner for answer

                    # Check if re-plan is needed (task failed)
                    elif snap_state.get("needs_replan"):
                        await ws_send(ws, "replan_needed", {
                            "reason": snap_state.get("replan_reason", "A task failed"),
                        })
                        # Graph will route to planner automatically
                        if pending_state_out is not None:
                            pending_state_out.append(snap_state)
                        # Don't return — let the graph continue to planner
                    else:
                        # Check for next step approval
                        idx = snap_state.get("current_task_index", 0)
                        tasks = snap_state.get("tasks", [])
                        # Find next pending task (dependency-aware)
                        next_task = None
                        for t in tasks:
                            if t["status"] == "pending":
                                next_task = t
                                break
                        if next_task and not snap_state.get("step_approved"):
                            await ws_send(ws, "step_proposal", {
                                "step_index": idx,
                                "total_steps": len(tasks),
                                "task_id": next_task["id"],
                                "title": next_task["title"],
                                "description": next_task["description"],
                                "depends_on": next_task.get("depends_on", []),
                                "acceptance_criteria": next_task.get("acceptance_criteria", ""),
                            })
                            if pending_state_out is not None:
                                pending_state_out.append(snap_state)
                            return "waiting_for_step_approval"
                except Exception:
                    pass

            current_node = None

    # Graph finished — send final answer
    try:
        snap = graph.get_state(config)
        if snap and snap.values:
            final = snap.values.get("final_answer")
            if final:
                await ws_send(ws, "done", {"final_answer": final})
            else:
                await ws_send(ws, "done", {"final_answer": ""})
    except Exception:
        await ws_send(ws, "done", {"final_answer": ""})

    return "completed"


@app.get("/")
async def serve_ui():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action", "")

            if action == "start":
                goal = msg.get("goal", "").strip()
                api_key = msg.get("api_key") or OPENAI_API_KEY
                agent_id = msg.get("agent_id")  # Optional: use a specific agent

                if not goal:
                    await ws_send(ws, "error", {"message": "goal is required"})
                    continue

                if not api_key:
                    await ws_send(ws, "error", {"message": "OPENAI_API_KEY not configured"})
                    continue

                # Build graph: dynamic if agent_id provided, else default
                tool_schemas = []
                try:
                    agent_graph, agent_checkpointer, tool_schemas = await get_compiled_graph_for_agent(agent_id)
                except Exception:
                    agent_graph = default_compiled_graph
                    agent_checkpointer = default_checkpointer

                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}

                mode = msg.get("mode", "plan")

                # Build user_config from optional fields in the start message
                user_config = {}
                config_fields = [
                    "planner_model", "planner_temperature",
                    "executor_model", "executor_temperature",
                    "synthesizer_model", "direct_model",
                    "max_tokens", "system_prompt",
                    "enable_reflection", "max_retries",
                ]
                for field in config_fields:
                    if field in msg:
                        user_config[field] = msg[field]

                initial_state = {
                    "thread_id": thread_id,
                    "goal": goal,
                    "mode": mode,
                    "api_key": api_key,
                    "agent_id": agent_id,
                    "user_config": user_config,
                    "tasks": [],
                    "completed_tasks": [],
                    "current_task_index": 0,
                    "final_answer": None,
                    "needs_question": False,
                    "question_text": None,
                    "question_options": None,
                    "human_input": None,
                    "plan_approved": False,
                    "step_approved": False,
                    "step_feedback": None,
                    "direct_messages": [],
                    "built_steps": [],
                    "scratchpad": [],
                    "tool_schemas": tool_schemas,
                    "planner_thinking": [],
                    "plan_revision_count": 0,
                    "needs_replan": False,
                    "replan_reason": None,
                    "needs_worker_clarification": False,
                    "worker_question": None,
                    "worker_question_task_id": None,
                    "planner_response": None,
                }

                ws.pending_state = {**initial_state}
                ws.state_thread_id = thread_id
                ws.state_config = config
                ws.agent_graph = agent_graph  # Store for reuse in answer/approve

                await ws_send(ws, "thread_created", {"thread_id": thread_id, "agent_id": agent_id})

                try:
                    pending = []
                    result = await run_graph_and_stream(ws, initial_state, config, agent_graph, pending)

                    if result == "waiting_for_approval":
                        ws.pending_state = pending[0] if pending else {**initial_state}
                    elif result == "waiting_for_next":
                        ws.pending_state = pending[0] if pending else {**initial_state}
                    elif result == "waiting_for_answer":
                        pass

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Graph error: {str(e)}"})

            elif action == "answer":
                answer = msg.get("answer", "")
                base_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)

                if not base_state:
                    await ws_send(ws, "error", {"message": "No active session to resume. Please start again."})
                    continue

                try:
                    resume_state = {
                        **base_state,
                        "human_input": answer,
                        "needs_question": False,
                        "question_text": None,
                        "question_options": None,
                        "plan_approved": False,
                        "tasks": [],
                        "completed_tasks": [],
                        "current_task_index": 0,
                    }

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}

                    await ws_send(ws, "status", {"message": "Updating plan with your answer..."})
                    pending = []
                    result = await run_graph_and_stream(ws, resume_state, new_config, agent_graph, pending)

                    if result == "waiting_for_approval":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = pending[0] if pending else resume_state
                    elif result == "waiting_for_answer":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = resume_state

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Resume error: {str(e)}"})

            elif action == "approve":
                pending_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)

                if not pending_state:
                    await ws_send(ws, "error", {"message": "No pending plan to approve"})
                    continue

                try:
                    resume_state = {
                        **pending_state,
                        "plan_approved": True,
                        "current_task_index": 0,
                        "completed_tasks": [],
                        "step_approved": True,   # approve the first step immediately
                        "step_feedback": None,
                    }

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}

                    ws.pending_state = None

                    await ws_send(ws, "status", {"message": "Starting execution..."})
                    pending = []
                    result = await run_graph_and_stream(ws, resume_state, new_config, agent_graph, pending)

                    if result == "waiting_for_step_approval":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = pending[0] if pending else resume_state
                    elif result in ("waiting_for_answer", "waiting_for_approval"):
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[APPROVE ERROR]\n{tb}")
                    await ws_send(ws, "error", {"message": f"Approve error: {str(e)}", "traceback": tb})

            elif action == "next_step":
                pending_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)
                user_message = msg.get("message", "").strip()

                if not pending_state:
                    await ws_send(ws, "error", {"message": "No active direct session"})
                    continue

                try:
                    resume_state = {
                        **pending_state,
                        "human_input": user_message or "continue",
                        "needs_question": False,
                        "question_text": None,
                        "mode": "direct",
                    }

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}
                    ws.pending_state = None

                    await ws_send(ws, "status", {"message": "Building next step..."})
                    pending = []
                    result = await run_graph_and_stream(ws, resume_state, new_config, agent_graph, pending)

                    if result == "waiting_for_next":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = pending[0] if pending else resume_state

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[NEXT STEP ERROR]\n{tb}")
                    await ws_send(ws, "error", {"message": f"Next step error: {str(e)}"})

            elif action == "step_approve":
                pending_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)

                if not pending_state:
                    await ws_send(ws, "error", {"message": "No pending step to approve"})
                    continue

                try:
                    resume_state = {
                        **pending_state,
                        "step_approved": True,
                        "step_feedback": None,
                    }

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}
                    ws.pending_state = None

                    await ws_send(ws, "status", {"message": "Executing step..."})
                    pending = []
                    result = await run_graph_and_stream(ws, resume_state, new_config, agent_graph, pending)

                    if result == "waiting_for_step_approval":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = pending[0] if pending else resume_state
                    elif result in ("waiting_for_answer", "waiting_for_approval"):
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[STEP APPROVE ERROR]\n{tb}")
                    await ws_send(ws, "error", {"message": f"Step approve error: {str(e)}"})

            elif action == "edit_plan":
                pending_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)
                edited_tasks = msg.get("tasks", [])

                if not pending_state:
                    await ws_send(ws, "error", {"message": "No pending plan to edit"})
                    continue

                if not edited_tasks:
                    await ws_send(ws, "error", {"message": "No tasks provided"})
                    continue

                try:
                    # User edited the plan — update tasks in pending state
                    updated_tasks = []
                    for t in edited_tasks:
                        updated_tasks.append({
                            "id": t.get("id", str(uuid.uuid4())[:8]),
                            "title": t["title"],
                            "description": t["description"],
                            "tool_name": t.get("tool_name"),
                            "status": "pending",
                            "result": None,
                            "depends_on": t.get("depends_on", []),
                            "priority": t.get("priority", "medium"),
                            "acceptance_criteria": t.get("acceptance_criteria", "Task completed successfully"),
                            "estimated_complexity": t.get("estimated_complexity", "moderate"),
                            "reflection": None,
                        })

                    ws.pending_state = {
                        **pending_state,
                        "tasks": updated_tasks,
                        "current_task_index": 0,
                    }

                    await ws_send(ws, "plan_ready", {
                        "tasks": [
                            {
                                "id": t["id"],
                                "title": t["title"],
                                "description": t["description"],
                                "status": t["status"],
                                "depends_on": t.get("depends_on", []),
                                "priority": t.get("priority", "medium"),
                                "acceptance_criteria": t.get("acceptance_criteria", ""),
                                "estimated_complexity": t.get("estimated_complexity", "moderate"),
                            }
                            for t in updated_tasks
                        ],
                        "is_replan": False,
                        "is_user_edit": True,
                    })

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[EDIT PLAN ERROR]\n{tb}")
                    await ws_send(ws, "error", {"message": f"Edit plan error: {str(e)}"})

            elif action == "step_reject":
                pending_state = getattr(ws, "pending_state", None)
                agent_graph = getattr(ws, "agent_graph", default_compiled_graph)
                feedback = msg.get("feedback", "").strip()

                if not pending_state:
                    await ws_send(ws, "error", {"message": "No pending step to reject"})
                    continue

                try:
                    tasks = list(pending_state.get("tasks", []))
                    idx = pending_state.get("current_task_index", 0)

                    if feedback and idx < len(tasks):
                        # Mark current task as skipped with user feedback
                        tasks[idx] = {**tasks[idx], "status": "skipped", "result": f"Skipped by user: {feedback}"}
                        completed = list(pending_state.get("completed_tasks", []))
                        completed.append({"title": tasks[idx]["title"], "result": f"Skipped: {feedback}"})

                        resume_state = {
                            **pending_state,
                            "tasks": tasks,
                            "completed_tasks": completed,
                            "current_task_index": idx + 1,
                            "step_approved": False,
                            "step_feedback": feedback,
                        }
                    else:
                        # No feedback — just skip to next step
                        if idx < len(tasks):
                            tasks[idx] = {**tasks[idx], "status": "skipped", "result": "Skipped by user"}
                        resume_state = {
                            **pending_state,
                            "tasks": tasks,
                            "current_task_index": idx + 1,
                            "step_approved": False,
                            "step_feedback": None,
                        }

                    await ws_send(ws, "step_skipped", {
                        "step_index": idx,
                        "task_id": tasks[idx]["id"] if idx < len(tasks) else "",
                        "feedback": feedback,
                    })

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}
                    ws.pending_state = None

                    pending = []
                    result = await run_graph_and_stream(ws, resume_state, new_config, agent_graph, pending)

                    if result == "waiting_for_step_approval":
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id
                        ws.pending_state = pending[0] if pending else resume_state
                    elif result in ("waiting_for_answer", "waiting_for_approval"):
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[STEP REJECT ERROR]\n{tb}")
                    await ws_send(ws, "error", {"message": f"Step reject error: {str(e)}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws_send(ws, "error", {"message": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)
