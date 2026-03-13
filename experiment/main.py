import json
import os
import sys
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Add experiment dir to path so graph package is importable
sys.path.insert(0, os.path.dirname(__file__))

from graph.builder import build_graph

load_dotenv()

app = FastAPI(title="Experiment: LangGraph AI Planner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Build graph once at startup
compiled_graph, checkpointer = build_graph()


async def ws_send(ws: WebSocket, event: str, data: dict):
    """Send a JSON event over WebSocket."""
    await ws.send_text(json.dumps({"event": event, "data": data}))


async def run_graph_and_stream(ws: WebSocket, state: dict, config: dict, pending_state_out: list | None = None):
    """Run the graph and stream events to the WebSocket client."""
    thread_id = config["configurable"]["thread_id"]

    # Stream events from the graph
    current_node = None
    current_task_id = None
    streamed_text = ""

    async for event in compiled_graph.astream_events(state, config, version="v2"):
        kind = event.get("event", "")
        name = event.get("name", "")
        tags = event.get("tags", [])

        # Track which node we're in
        if kind == "on_chain_start" and name in ("planner", "executor", "synthesizer"):
            current_node = name

            if name == "planner":
                await ws_send(ws, "status", {"message": "Creating plan..."})
            elif name == "executor":
                # Read task index from the node's input (available in astream_events v2)
                try:
                    node_input = event.get("data", {}).get("input", {})
                    task_idx = node_input.get("current_task_index", 0)
                    tasks = node_input.get("tasks", [])
                    if task_idx < len(tasks):
                        current_task_id = tasks[task_idx]["id"]
                        await ws_send(ws, "task_start", {
                            "task_id": current_task_id,
                            "title": tasks[task_idx]["title"],
                        })
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
                # Stream tool call name to frontend as soon as LLM starts calling a tool
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
        if kind == "on_chain_end" and name in ("planner", "executor", "synthesizer"):
            if name == "planner":
                # Read planner output directly from the event (avoids stale checkpointer reads)
                try:
                    planner_output = event.get("data", {}).get("output", {})
                    if planner_output.get("needs_question"):
                        await ws_send(ws, "question", {
                            "text": planner_output.get("question_text", ""),
                            "options": planner_output.get("question_options", []),
                        })
                        return "waiting_for_answer"
                    else:
                        tasks = planner_output.get("tasks", [])
                        if tasks:
                            await ws_send(ws, "plan_ready", {
                                "tasks": [
                                    {"id": t["id"], "title": t["title"], "description": t["description"], "status": t["status"]}
                                    for t in tasks
                                ]
                            })
                            # Store the full state needed for approval
                            if pending_state_out is not None:
                                pending_state_out.append({
                                    **state,
                                    "tasks": tasks,
                                    "needs_question": False,
                                    "plan_approved": False,
                                    "current_task_index": 0,
                                    "completed_tasks": [],
                                    "human_input": planner_output.get("human_input", state.get("human_input")),
                                })
                            return "waiting_for_approval"
                except Exception:
                    pass

            elif name == "executor" and current_task_id:
                await ws_send(ws, "task_done", {"task_id": current_task_id})
                current_task_id = None

            current_node = None

    # Graph finished — send final answer
    try:
        snap = compiled_graph.get_state(config)
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

                if not goal:
                    await ws_send(ws, "error", {"message": "goal is required"})
                    continue

                if not api_key:
                    await ws_send(ws, "error", {"message": "OPENAI_API_KEY not configured"})
                    continue

                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}

                initial_state = {
                    "thread_id": thread_id,
                    "goal": goal,
                    "mode": "plan",
                    "api_key": api_key,
                    "tasks": [],
                    "completed_tasks": [],
                    "current_task_index": 0,
                    "final_answer": None,
                    "needs_question": False,
                    "question_text": None,
                    "question_options": None,
                    "human_input": None,
                    "plan_approved": False,
                }

                # Always store state upfront so answer/approve can always access it
                ws.pending_state = {**initial_state}
                ws.state_thread_id = thread_id
                ws.state_config = config

                await ws_send(ws, "thread_created", {"thread_id": thread_id})

                try:
                    pending = []
                    result = await run_graph_and_stream(ws, initial_state, config, pending)

                    if result == "waiting_for_approval":
                        ws.pending_state = pending[0] if pending else {**initial_state}
                    elif result == "waiting_for_answer":
                        pass  # ws.pending_state already set to initial_state above

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Graph error: {str(e)}"})

            elif action == "answer":
                # User answered a clarifying question — re-plan with their answer
                answer = msg.get("answer", "")

                # Use the stored initial state, patch in the answer
                base_state = getattr(ws, "pending_state", None)
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
                    result = await run_graph_and_stream(ws, resume_state, new_config, pending)

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
                # User approved the plan — use stored pending state, set plan_approved=True
                pending_state = getattr(ws, "pending_state", None)
                if not pending_state:
                    await ws_send(ws, "error", {"message": "No pending plan to approve"})
                    continue

                try:
                    resume_state = {
                        **pending_state,
                        "plan_approved": True,
                        "current_task_index": 0,
                        "completed_tasks": [],
                    }

                    new_thread_id = str(uuid.uuid4())
                    new_config = {"configurable": {"thread_id": new_thread_id}}

                    ws.pending_state = None  # clear after use

                    await ws_send(ws, "status", {"message": "Starting execution..."})
                    result = await run_graph_and_stream(ws, resume_state, new_config)

                    if result in ("waiting_for_answer", "waiting_for_approval"):
                        ws.state_config = new_config
                        ws.state_thread_id = new_thread_id

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Approve error: {str(e)}"})

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
