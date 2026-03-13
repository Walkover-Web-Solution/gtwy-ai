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


async def run_graph_and_stream(ws: WebSocket, state: dict, config: dict):
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
        if kind == "on_chain_start" and name in ("planner", "executor", "direct", "synthesizer"):
            current_node = name

            if name == "planner":
                await ws_send(ws, "status", {"message": "Creating plan..."})
            elif name == "executor":
                # Figure out which task is being executed
                task_idx = state.get("current_task_index", 0)
                # Get latest state from checkpointer
                try:
                    snap = compiled_graph.get_state(config)
                    if snap and snap.values:
                        task_idx = snap.values.get("current_task_index", 0)
                        tasks = snap.values.get("tasks", [])
                        if task_idx < len(tasks):
                            current_task_id = tasks[task_idx]["id"]
                            await ws_send(ws, "task_start", {
                                "task_id": current_task_id,
                                "title": tasks[task_idx]["title"],
                            })
                except Exception:
                    pass
                streamed_text = ""
            elif name == "direct":
                await ws_send(ws, "status", {"message": "Generating response..."})
                streamed_text = ""
            elif name == "synthesizer":
                await ws_send(ws, "status", {"message": "Preparing final output..."})
                streamed_text = ""

        # Stream LLM token chunks
        if kind == "on_chat_model_stream":
            chunk = event.get("data", {})
            content = ""
            if hasattr(chunk, "content"):
                content = chunk.content
            elif isinstance(chunk, dict):
                content = chunk.get("chunk", {})
                if hasattr(content, "content"):
                    content = content.content
                elif isinstance(content, str):
                    pass
                else:
                    content = ""

            if content:
                streamed_text += content
                if current_node == "executor" and current_task_id:
                    await ws_send(ws, "task_chunk", {"task_id": current_task_id, "chunk": content})
                elif current_node == "direct":
                    await ws_send(ws, "direct_chunk", {"chunk": content})
                elif current_node == "synthesizer":
                    await ws_send(ws, "final_chunk", {"chunk": content})

        # Node completed
        if kind == "on_chain_end" and name in ("planner", "executor", "direct", "synthesizer"):
            if name == "planner":
                # Check result — did planner produce tasks or a question?
                try:
                    snap = compiled_graph.get_state(config)
                    if snap and snap.values:
                        vals = snap.values
                        if vals.get("needs_question"):
                            await ws_send(ws, "question", {
                                "text": vals.get("question_text", ""),
                                "options": vals.get("question_options", []),
                            })
                            # Graph will end here (edges route to END on question)
                            # WebSocket stays open — we wait for user answer
                            return "waiting_for_answer"
                        else:
                            tasks = vals.get("tasks", [])
                            if tasks:
                                await ws_send(ws, "plan_ready", {
                                    "tasks": [
                                        {"id": t["id"], "title": t["title"], "description": t["description"], "status": t["status"]}
                                        for t in tasks
                                    ]
                                })
                except Exception:
                    pass

            elif name == "executor" and current_task_id:
                await ws_send(ws, "task_done", {"task_id": current_task_id})
                current_task_id = None

            elif name == "direct":
                pass  # final_answer is set in state

            elif name == "synthesizer":
                pass  # final_answer is set in state

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
                mode = msg.get("mode", "direct")  # "plan" or "direct"
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
                    "mode": mode,
                    "api_key": api_key,
                    "tasks": [],
                    "completed_tasks": [],
                    "current_task_index": 0,
                    "final_answer": None,
                    "needs_question": False,
                    "question_text": None,
                    "question_options": None,
                    "human_input": None,
                }

                await ws_send(ws, "thread_created", {"thread_id": thread_id, "mode": mode})

                try:
                    result = await run_graph_and_stream(ws, initial_state, config)

                    # If waiting for answer, store config for this connection
                    if result == "waiting_for_answer":
                        ws.state_config = config
                        ws.state_thread_id = thread_id

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Graph error: {str(e)}"})

            elif action == "answer":
                # User answered a question — resume the graph
                answer = msg.get("answer", "")
                thread_id = msg.get("thread_id") or getattr(ws, "state_thread_id", None)

                if not thread_id:
                    await ws_send(ws, "error", {"message": "No active thread to resume"})
                    continue

                config = getattr(ws, "state_config", None) or {
                    "configurable": {"thread_id": thread_id}
                }

                # Get current state and update with human answer
                try:
                    snap = compiled_graph.get_state(config)
                    if snap and snap.values:
                        # Re-run with the answer — start fresh from planner with human_input
                        resume_state = {
                            **snap.values,
                            "human_input": answer,
                            "needs_question": False,
                            "question_text": None,
                            "question_options": None,
                            "mode": "plan",  # force back to plan mode
                        }

                        # New thread for the resumed run (planner will use human_input)
                        new_thread_id = str(uuid.uuid4())
                        new_config = {"configurable": {"thread_id": new_thread_id}}

                        await ws_send(ws, "status", {"message": "Resuming with your answer..."})
                        result = await run_graph_and_stream(ws, resume_state, new_config)

                        if result == "waiting_for_answer":
                            ws.state_config = new_config
                            ws.state_thread_id = new_thread_id

                except Exception as e:
                    await ws_send(ws, "error", {"message": f"Resume error: {str(e)}"})

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
