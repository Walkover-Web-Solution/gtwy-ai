import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse
from langgraph.types import Command
from langgraph.checkpoint.mongodb import MongoDBSaver
from models.mongo_connection import client as async_mongo_client

from config import Config

_checkpoint_mongo_client = async_mongo_client.delegate

from globals import logger
from workflow.builder import build_workflow_graph
from workflow.tool_adapter import build_langchain_tools
from src.services.session_manager import (
    publish_workflow_event,
    register_session_in_redis,
    subscribe_to_human_input,
    unregister_session_from_redis,
)
from src.services.utils.common_utils import (
    create_latency_object,
    process_background_tasks,
    process_background_tasks_for_playground,
    update_usage_metrics,
)

_NODE_NAMES = ("planner", "executor", "synthesizer")
_QUESTION_INTERRUPT_TYPES = {"planner_question", "worker_clarification"}


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

@dataclass
class WorkflowSession:
    run_id: str
    graph: object
    config: dict
    parsed_data: dict
    bridge_configurations: dict
    tool_schemas: list[dict] = field(default_factory=list)
    last_state: dict = field(default_factory=dict)


class SessionRegistry:
    """Encapsulates workflow session state, WebSocket connections, and input queues.

    NOTE: This is an in-process registry. In a multi-worker deployment (gunicorn/uvicorn
    with multiple workers), sessions created on worker A won't exist on worker B.
    Human input and WebSocket reconnects will break if routed to a different worker.
    Use sticky sessions at the load balancer level, or migrate to a shared store
    (Redis/MongoDB) for production multi-worker deployments.
    """

    def __init__(self):
        self._sessions: dict[str, WorkflowSession] = {}
        self._ws_connections: dict[str, Any] = {}
        self._input_queues: dict[str, asyncio.Queue] = {}

    def register(self, session: WorkflowSession) -> None:
        self._sessions[session.run_id] = session

    def get(self, run_id: str) -> WorkflowSession | None:
        return self._sessions.get(run_id)

    def remove(self, run_id: str) -> None:
        self._sessions.pop(run_id, None)
        self._ws_connections.pop(run_id, None)
        self._input_queues.pop(run_id, None)

    def set_ws(self, run_id: str, ws: Any) -> None:
        self._ws_connections[run_id] = ws

    def get_ws(self, run_id: str) -> Any:
        return self._ws_connections.get(run_id)

    def remove_ws(self, run_id: str) -> None:
        self._ws_connections.pop(run_id, None)

    def create_input_queue(self, run_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._input_queues[run_id] = queue
        return queue

    def get_input_queue(self, run_id: str) -> asyncio.Queue | None:
        return self._input_queues.get(run_id)

    def remove_input_queue(self, run_id: str) -> None:
        self._input_queues.pop(run_id, None)


registry = SessionRegistry()

# Backward-compatible aliases for external consumers (e.g., modelRouter.py)
WORKFLOW_SESSIONS = registry._sessions
WS_CONNECTIONS = registry._ws_connections
HUMAN_INPUT_QUEUES = registry._input_queues


# ---------------------------------------------------------------------------
# State / response builders
# ---------------------------------------------------------------------------

def _build_initial_state(parsed_data: dict, tool_schemas: list[dict], run_id: str) -> dict:
    return {
        "run_id": run_id,
        "thread_id": parsed_data.get("thread_id"),
        "goal": parsed_data.get("user", ""),
        "api_key": parsed_data.get("apikey"),
        "user_config": {
            "planner_model": parsed_data.get("model"),
            "planner_service": parsed_data.get("service", "openai"),
            "planner_temperature": parsed_data.get("configuration", {}).get("temperature", 0.3),
            "executor_model": parsed_data.get("model"),
            "executor_service": parsed_data.get("service", "openai"),
            "executor_temperature": parsed_data.get("configuration", {}).get("temperature", 0.4),
            "synthesizer_model": parsed_data.get("model"),
            "synthesizer_service": parsed_data.get("service", "openai"),
            "require_plan_approval": parsed_data.get("configuration", {}).get("require_plan_approval", True),
            "require_step_approval": parsed_data.get("configuration", {}).get("require_step_approval", False),
            "system_prompt": parsed_data.get("configuration", {}).get("prompt", ""),
            "enable_reflection": parsed_data.get("configuration", {}).get("enable_reflection", False),
            "max_retries": parsed_data.get("configuration", {}).get("max_retries", 3),
        },
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
        "worker_messages": None,
        "runtime_variables": parsed_data.get("variables") or {},
        "response_schema": None,
    }

def _build_workflow_response_payload(
    run_id: str,
    final_state: dict,
    interrupt_payload: dict | None = None,
) -> tuple[str, dict]:
    is_interrupted = bool(interrupt_payload)
    content = json.dumps(interrupt_payload) if is_interrupted else (final_state.get("final_answer") or "")
    return content, {
        "type": "text",
        "data": {
            "content": content,
            "model": final_state.get("user_config", {}).get("planner_model"),
            "workflow_mode": "advance",
            "workflow_status": "waiting" if is_interrupted else "completed",
            "run_id": run_id,
            "needs_question": bool(interrupt_payload and interrupt_payload.get("type") in _QUESTION_INTERRUPT_TYPES),
            "pending_interrupt": interrupt_payload,
            "tasks": final_state.get("tasks", []),
            "planner_thinking": final_state.get("planner_thinking", []),
        },
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _extract_tool_calls_from_state(final_state: dict) -> list[dict]:
    """Extract tool call information from workflow state in the same format as direct flow.
    
    Direct flow format: list of dicts where each dict has tool_call_id as key:
    [
        {
            "tool_call_id": {
                "name": "tool_name",
                "args": {...},
                "data": {...},
                "id": "tool_id"
            }
        }
    ]
    """
    tool_calls_list = []
    tasks = final_state.get("tasks", [])
    
    for task in tasks:
        if task.get("status") in ("completed", "failed"):
            tool_name = task.get("tool_name", "unknown")
            task_id = task.get("id", "")
            result = task.get("result", "")
            
            # Parse result to extract actual response data
            try:
                if isinstance(result, str) and result.strip().startswith("{"):
                    result_data = json.loads(result)
                else:
                    result_data = result
            except:
                result_data = result
            
            # Build tool call entry matching direct flow format
            tool_call_entry = {
                task_id: {
                    "name": tool_name,
                    "args": {
                        "title": task.get("title", ""),
                        "description": task.get("description", ""),
                    },
                    "data": {
                        "response": result_data if task.get("status") == "completed" else {"error": result_data},
                        "metadata": {
                            "type": "workflow_task",
                            "status": task.get("status"),
                            "complexity": task.get("estimated_complexity", "moderate"),
                        },
                        "status": 1 if task.get("status") == "completed" else 0,
                    },
                    "id": tool_name,
                }
            }
            tool_calls_list.append(tool_call_entry)
    
    return tool_calls_list


def _format_workflow_message(final_state: dict, interrupt_payload: dict | None) -> str:
    """Format the AI message for the user in a clean, friendly way."""
    if interrupt_payload:
        interrupt_type = interrupt_payload.get("type", "")

        if interrupt_type == "planner_question":
            question = interrupt_payload.get("question", "")
            options = interrupt_payload.get("options", [])
            thinking = interrupt_payload.get("planner_thinking", [])

            msg_parts = []
            # Show brief reasoning context if available
            reasoning = [s.get("content", "") for s in (thinking or []) if s.get("type") in ("reasoning", "plan_reasoning") and s.get("content")]
            if reasoning:
                msg_parts.append(reasoning[-1])
                msg_parts.append("")

            msg_parts.append(question)

            if options:
                msg_parts.append("")
                for i, opt in enumerate(options, 1):
                    msg_parts.append(f"  {i}. {opt}")

            return "\n".join(msg_parts)

        elif interrupt_type == "worker_clarification":
            question = interrupt_payload.get("question", "")
            # Find task title from state for friendlier message
            task_id = interrupt_payload.get("task_id", "")
            task_title = ""
            for t in final_state.get("tasks", []):
                if t.get("id") == task_id:
                    task_title = t.get("title", "")
                    break
            if task_title:
                return f"While working on \"{task_title}\", I need some help:\n\n{question}"
            return f"I need some clarification to continue:\n\n{question}"

        elif interrupt_type == "step_approval":
            task = interrupt_payload.get("task", {})
            title = task.get("title", "")
            description = task.get("description", "")
            tool = task.get("tool_name", "")
            msg_parts = [f"I'm about to run the next step: **{title}**"]
            if description:
                msg_parts.append(f"\n{description}")
            if tool:
                msg_parts.append(f"\nTool: `{tool}`")
            msg_parts.append("\nShould I proceed?")
            return "\n".join(msg_parts)

        elif interrupt_type == "plan_approval":
            tasks = interrupt_payload.get("tasks", [])
            thinking = interrupt_payload.get("planner_thinking", [])
            goal = interrupt_payload.get("goal", "")

            msg_parts = []
            # Show reasoning summary if present
            reasoning = [s.get("content", "") for s in (thinking or []) if s.get("type") in ("reasoning", "plan_reasoning") and s.get("content")]
            if reasoning:
                msg_parts.append(reasoning[-1])
                msg_parts.append("")

            msg_parts.append(f"Here's my plan ({len(tasks)} step{'s' if len(tasks) != 1 else ''}):\n")
            for i, task in enumerate(tasks, 1):
                title = task.get("title", f"Step {i}")
                tool = task.get("tool_name", "")
                tool_tag = f" (using `{tool}`)" if tool else ""
                msg_parts.append(f"  {i}. **{title}**{tool_tag}")
            msg_parts.append("\nDo you approve this plan?")
            return "\n".join(msg_parts)

    # No interrupt — return final answer
    final_answer = final_state.get("final_answer", "")
    if final_answer:
        return final_answer

    # Fallback: task completion summary
    completed = [t for t in final_state.get("tasks", []) if t.get("status") == "completed"]
    if completed:
        msg_parts = [f"Completed {len(completed)} task{'s' if len(completed) != 1 else ''}:"]
        for task in completed:
            result_preview = (task.get("result", "") or "")[:200]
            msg_parts.append(f"  - **{task.get('title', '')}**: {result_preview}")
        return "\n".join(msg_parts)

    return "Working on it..."


def _build_history_params(session: WorkflowSession, content: str, message_id: str, response: dict) -> dict:
    pd = session.parsed_data
    final_state = session.last_state
    interrupt_payload = response.get("data", {}).get("pending_interrupt")
    
    # Extract tool calls from completed tasks
    tool_calls = _extract_tool_calls_from_state(final_state)
    
    # Format the AI message properly
    ai_message = _format_workflow_message(final_state, interrupt_payload)
    
    # Get runtime variables from workflow state (updated during execution)
    runtime_variables = final_state.get("runtime_variables") or pd.get("variables") or {}
    
    return {
        "thread_id": pd.get("thread_id"),
        "sub_thread_id": pd.get("sub_thread_id"),
        "user": pd.get("original_user") or pd.get("user") or "",
        "message": ai_message,
        "org_id": pd.get("org_id"),
        "bridge_id": pd.get("bridge_id"),
        "model": pd.get("model"),
        "service": pd.get("service"),
        "channel": "chat",
        "type": "assistant",
        "actor": "user",
        "tools": {},
        "chatbot_message": "",
        "tools_call_data": tool_calls,
        "message_id": message_id,
        "llm_urls": [],
        "revised_prompt": None,
        "user_urls": [],
        "AiConfig": pd.get("configuration"),
        "firstAttemptError": "",
        "annotations": [],
        "fallback_model": "",
        "response": response,
        "folder_id": pd.get("folder_id"),
        "prompt": (pd.get("configuration") or {}).get("prompt"),
        "variables": runtime_variables,
    }


# ---------------------------------------------------------------------------
# Emit helper  (WebSocket only)
# ---------------------------------------------------------------------------

async def _emit_to_ws(run_id: str, event: str, node: str, data: dict) -> None:
    # Publish to Redis for cross-worker WS event relay
    await publish_workflow_event(run_id, event, node, data)

    ws = registry.get_ws(run_id)
    if not ws:
        return
    try:
        await ws.send_json({"event": event, "node": node, "run_id": run_id, "data": data})
    except Exception:
        pass


async def _wait_for_human_input(run_id: str) -> None:
    """Wait for a human answer from the WS endpoint and resume the workflow."""
    queue = registry.create_input_queue(run_id)
    sub_task = None
    try:
        logger.info(f"[Workflow] waiting for human input on run_id={run_id}")
        # subscribe_to_human_input blocks until an answer arrives via Redis and
        # puts it into queue. Handles its own 600s timeout internally.
        sub_task = asyncio.create_task(subscribe_to_human_input(run_id, queue))
        resume_value = await asyncio.wait_for(queue.get(), timeout=605)
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
        logger.info(f"[Workflow] human input received for run_id={run_id}: {resume_value!r}")
        asyncio.create_task(resume_advanced_workflow(run_id, resume_value))
    except asyncio.TimeoutError:
        logger.warning(f"[Workflow] human input timeout for run_id={run_id}")
        if sub_task:
            sub_task.cancel()
            try:
                await sub_task
            except asyncio.CancelledError:
                pass
        # Don't remove session — keep for recovery from checkpoint if user reconnects
        registry.remove_ws(run_id)
    finally:
        HUMAN_INPUT_QUEUES.pop(run_id, None)


# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------

async def _save_history(result: dict, session: WorkflowSession, params: dict = None, thread_info=None, transfer_request_id=None) -> None:
    parsed_data = session.parsed_data
    try:
        if parsed_data.get("is_playground"):
            await process_background_tasks_for_playground(result, parsed_data)
        else:
            await process_background_tasks(
                parsed_data,
                result,
                params or {},
                thread_info,
                transfer_request_id,
                session.bridge_configurations,
            )
    except Exception as e:
        logger.error(f"workflow history save error: {e}")


# ---------------------------------------------------------------------------
# Graph invocation helpers
# ---------------------------------------------------------------------------


async def _handle_chain_start_executor(event: dict, emit) -> list[str]:
    try:
        tasks = event.get("data", {}).get("input", {}).get("tasks", [])
        completed_ids = {t["id"] for t in tasks if t["status"] in ("completed", "skipped")}
        running = [
            t["id"] for t in tasks
            if t["status"] == "pending" and all(d in completed_ids for d in t.get("depends_on", []))
        ]
        for t in tasks:
            if t["id"] in running:
                await emit("task_start", "executor", {"task_id": t["id"], "title": t["title"]})
        return running
    except Exception:
        return []


async def _handle_chain_end_planner(event: dict, emit) -> None:
    try:
        output = event.get("data", {}).get("output", {})
        if output.get("planner_thinking"):
            await emit("thinking", "planner", {"steps": output["planner_thinking"]})
        if output.get("needs_question"):
            pass  # handled via interrupt in stream_and_emit_workflow
        elif output.get("tasks"):
            await emit("plan_ready", "planner", {
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
                        "estimated_complexity": t.get("estimated_complexity", "simple"),
                    }
                    for t in output["tasks"]
                ],
            })
    except Exception:
        pass


async def _handle_chain_end_executor(event: dict, running_task_ids: list[str], emit) -> None:
    try:
        output = event.get("data", {}).get("output", {})
        for t in output.get("tasks", []):
            if t["id"] in running_task_ids:
                if t["status"] == "completed":
                    await emit("task_done", "executor", {"task_id": t["id"], "result": t.get("result", "")})
                elif t["status"] == "failed":
                    await emit("task_failed", "executor", {"task_id": t["id"], "error": t.get("result", "")})
        if output.get("needs_replan"):
            await emit("replan", "executor", {"reason": output.get("replan_reason", "")})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_ttl_index_created = False


def _ensure_ttl_index() -> None:
    """Create TTL index on checkpoint collections once. MongoDB auto-deletes expired docs."""
    global _ttl_index_created
    if _ttl_index_created:
        return
    try:
        db = _checkpoint_mongo_client[Config.MONGODB_DATABASE_NAME]
        for collection_name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            collection = db[collection_name]
            collection.create_index("updated_at", expireAfterSeconds=_TTL_SECONDS)
        _ttl_index_created = True
        logger.info("[Workflow] MongoDB TTL indexes created (auto-expire after 7 days)")
    except Exception as e:
        logger.warning(f"[Workflow] TTL index creation failed (non-critical): {e}")


async def create_advanced_workflow_session(parsed_data: dict, bridge_configurations: dict) -> WorkflowSession:
    research_tools, execution_tools, tool_schemas = build_langchain_tools(parsed_data, bridge_configurations)

    _ensure_ttl_index()
    checkpointer = MongoDBSaver(_checkpoint_mongo_client, Config.MONGODB_DATABASE_NAME)

    compiled_graph = build_workflow_graph(research_tools, execution_tools, tool_schemas, checkpointer)

    # Use thread_id + sub_thread_id as session/connection key and LangGraph thread key
    thread_id = parsed_data.get('thread_id')
    sub_thread_id = parsed_data.get('sub_thread_id')
    run_id = f"{thread_id}_{sub_thread_id}"
    thread_key = f"{thread_id}:{sub_thread_id}"
    session = WorkflowSession(
        run_id=run_id,
        graph=compiled_graph,
        config={"configurable": {"thread_id": thread_key}},
        parsed_data=parsed_data,
        bridge_configurations=bridge_configurations,
        tool_schemas=tool_schemas,
    )
    registry.register(session)
    await register_session_in_redis(run_id)
    return session


# ---------------------------------------------------------------------------
# Main streaming runner
# ---------------------------------------------------------------------------

async def stream_and_emit_workflow(
    session: WorkflowSession,
    initial_state: Any,
    message_id: str,
) -> dict:
    """
    Stream LangGraph events and push to frontend via WebSocket.

    Events: node_start, thinking, plan_ready, task_start, task_done,
      task_failed, tool_call, chunk, replan, interrupt, done, error
    """
    graph = session.graph
    running_task_ids: list[str] = []
    final_answer = ""
    interrupt_payload = None
    final_state: dict = {}

    async def emit(ev_name: str, node: str, data: dict) -> None:
        await _emit_to_ws(session.run_id, ev_name, node, data)

    try:
        async for event in graph.astream_events(initial_state, session.config, version="v2"):
            kind = event.get("event", "")
            event_name = event.get("name", "")
            streaming_node = event.get("metadata", {}).get("langgraph_node", "")

            if kind == "on_chain_start" and event_name in _NODE_NAMES:
                if event_name == "planner":
                    await emit("node_start", "planner", {})
                elif event_name == "synthesizer":
                    await emit("node_start", "synthesizer", {})
                elif event_name == "executor":
                    running_task_ids = await _handle_chain_start_executor(event, emit)

            elif kind == "on_chat_model_stream":
                try:
                    chunk = event["data"]["chunk"]
                    content = getattr(chunk, "content", "") or ""
                    if content and streaming_node == "synthesizer":
                        await emit("chunk", "synthesizer", {"content": content})
                    elif content and streaming_node == "planner":
                        await emit("thinking", "planner", {"content": content})
                    elif streaming_node == "executor":
                        if content:
                            await emit("thinking", "executor", {"content": content})
                        tool_chunks = getattr(chunk, "tool_call_chunks", []) or []
                        if tool_chunks and tool_chunks[0].get("name"):
                            await emit("tool_call", "executor", {"tool": tool_chunks[0]["name"]})
                except Exception:
                    pass

            elif kind == "on_chain_end" and event_name in _NODE_NAMES:
                if event_name == "planner":
                    await _handle_chain_end_planner(event, emit)
                elif event_name == "executor":
                    await _handle_chain_end_executor(event, running_task_ids, emit)
                    running_task_ids = []

        snapshot = await graph.aget_state(session.config)
        final_state = dict(snapshot.values or {})
        session.last_state = final_state
        if snapshot.interrupts:
            interrupt_payload = snapshot.interrupts[0].value
            interrupt_type = interrupt_payload.get("type", "")
            interrupt_node = "executor" if interrupt_type in ("worker_clarification", "step_approval") else "planner"
            await emit("interrupt", interrupt_node, interrupt_payload)
            asyncio.create_task(_wait_for_human_input(session.run_id))
        final_answer = final_state.get("final_answer") or ""

    except Exception as e:
        await emit("error", "system", {"message": str(e)})
        raise

    content, response = _build_workflow_response_payload(session.run_id, final_state, interrupt_payload)
    response["data"]["message_id"] = message_id

    # Keep session alive after completion — checkpoint persists in MongoDB for 7 days.
    # If user reconnects or sends a follow-up, we can resume from checkpoint.
    # Only clean up WebSocket and input queue, not the session itself.
    if not interrupt_payload:
        registry.remove_ws(session.run_id)
        HUMAN_INPUT_QUEUES.pop(session.run_id, None)

    await emit("done", "system", {
        "final_answer": final_answer,
        "workflow_status": response["data"]["workflow_status"],
        "tasks": response["data"].get("tasks", []),
    })

    return {
        "success": True,
        "response": response,
        "modelResponse": {"content": content},
        "historyParams": _build_history_params(session, content, message_id, response),
        "workflow_state": final_state,
        "run_id": session.run_id,
        "pending_interrupt": interrupt_payload,
    }


# ---------------------------------------------------------------------------
# Public API: resume & execute
# ---------------------------------------------------------------------------

async def _recover_session(run_id: str, parsed_data: dict, bridge_configurations: dict) -> WorkflowSession | None:
    """Rebuild a session from MongoDB checkpoint when the in-process registry lost it.
    
    This handles: server restart, worker switch, connection drop, or timeout cleanup.
    The MongoDB checkpoint persists for 7 days, so we can always recover.
    """
    try:
        research_tools, execution_tools, tool_schemas = build_langchain_tools(parsed_data, bridge_configurations)
        _ensure_ttl_index()
        checkpointer = MongoDBSaver(_checkpoint_mongo_client, Config.MONGODB_DATABASE_NAME)
        compiled_graph = build_workflow_graph(research_tools, execution_tools, tool_schemas, checkpointer)

        thread_id = parsed_data.get("thread_id")
        sub_thread_id = parsed_data.get("sub_thread_id")
        thread_key = f"{thread_id}:{sub_thread_id}"

        session = WorkflowSession(
            run_id=run_id,
            graph=compiled_graph,
            config={"configurable": {"thread_id": thread_key}},
            parsed_data=parsed_data,
            bridge_configurations=bridge_configurations,
            tool_schemas=tool_schemas,
        )

        # Verify checkpoint exists
        snapshot = await compiled_graph.aget_state(session.config)
        if not snapshot or not snapshot.values:
            logger.warning(f"[Workflow] No checkpoint found for run_id={run_id}")
            return None

        session.last_state = dict(snapshot.values)
        registry.register(session)
        await register_session_in_redis(run_id)
        logger.info(f"[Workflow] Session recovered from checkpoint for run_id={run_id}")
        return session
    except Exception as e:
        logger.error(f"[Workflow] Session recovery failed for run_id={run_id}: {e}")
        return None


async def resume_advanced_workflow(run_id: str, resume_value: Any, parsed_data: dict = None, bridge_configurations: dict = None) -> dict:
    session = registry.get(run_id)
    if session is None and parsed_data and bridge_configurations:
        session = await _recover_session(run_id, parsed_data, bridge_configurations)
    if session is None:
        raise ValueError(f"Workflow session {run_id} not found — checkpoint may have expired")

    message_id = session.parsed_data.get("message_id", run_id)
    result = await stream_and_emit_workflow(session, Command(resume=resume_value), message_id)
    await _save_history(result, session)
    return result


async def execute_advanced_workflow(
    parsed_data: dict,
    bridge_configurations: dict,
    params: dict,
    timer,
    thread_info,
    transfer_request_id: str,
) -> JSONResponse:
    session = await create_advanced_workflow_session(parsed_data, bridge_configurations)

    # Check if there's an existing checkpoint for this run_id (reconnect / connection drop)
    existing_snapshot = await session.graph.aget_state(session.config)
    if existing_snapshot and existing_snapshot.values:
        prev_state = dict(existing_snapshot.values)
        session.last_state = prev_state
        logger.info(f"[Workflow] Resuming from existing checkpoint for run_id={session.run_id}")

        if existing_snapshot.next:
            # Workflow was interrupted mid-run — resume it with the new user message as input
            user_input = parsed_data.get("user", "")
            result = await stream_and_emit_workflow(session, Command(resume=user_input), parsed_data["message_id"])
        else:
            # Workflow completed previously — start fresh with new goal but carry context
            initial_state = _build_initial_state(parsed_data, session.tool_schemas, session.run_id)
            # Carry over completed context from previous run
            initial_state["scratchpad"] = prev_state.get("scratchpad", [])
            result = await stream_and_emit_workflow(session, initial_state, parsed_data["message_id"])
    else:
        # Fresh workflow — no prior checkpoint
        initial_state = _build_initial_state(parsed_data, session.tool_schemas, session.run_id)
        result = await stream_and_emit_workflow(session, initial_state, parsed_data["message_id"])

    if not result["success"]:
        raise ValueError(result)

    result["response"]["data"]["message_id"] = parsed_data["message_id"]

    latency = create_latency_object(timer, params)
    
    # Sync runtime_variables from workflow state back to parsed_data for history
    workflow_state = result.get("workflow_state", {})
    if workflow_state.get("runtime_variables"):
        parsed_data["variables"] = workflow_state["runtime_variables"]
    
    update_usage_metrics(parsed_data, params, latency, result=result, success=True)
    result["response"]["usage"]["cost"] = parsed_data.get("usage", {}).get("expectedCost", 0)

    await _save_history(result, session, params, thread_info, transfer_request_id)

    return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})