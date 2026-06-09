import asyncio
import json

from fastapi.responses import JSONResponse, StreamingResponse

from globals import logger
from src.services.commonServices.streaming_service import StreamingService
from src.services.todo import plan_store
from src.services.todo.executor.orchestrator import execute_plan
from src.services.todo.executor.ports import resume_task as resume_task_handler
from src.services.todo.plan_store import _get_tasks, _set_tasks
from src.services.todo import synthesizer_service
from src.db_services.metrics_service import publish_plan_history_update


def _extract_last_completed_result(plan):
    """Return the last completed task's result as a user-facing string, or "".

    Tasks are ordered by their numeric suffix (task_1, task_2, …). If the
    stored result is a JSON string with a `data` field, unwrap it; otherwise
    pass the result through verbatim. Returns "" when no completed task
    exists or the result is empty."""
    if not plan:
        return ""
    tasks = _get_tasks(plan)
    completed_tasks = {
        task_id: task for task_id, task in tasks.items()
        if task.get("status") == "completed"
    }
    if not completed_tasks:
        return ""

    sorted_task_ids = sorted(
        completed_tasks.keys(),
        key=lambda x: int(x.split("_")[1]) if "_" in x and x.split("_")[1].isdigit() else 0,
    )
    result = completed_tasks[sorted_task_ids[-1]].get("result", "")
    if result is None:
        return ""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return result
        if isinstance(parsed, dict) and "data" in parsed:
            data = parsed["data"]
            return data if isinstance(data, str) else json.dumps(data)
        return result
    return json.dumps(result)


_FE_TASK_FIELDS = (
    "id", "title", "task_description", "dependencies",
    "status", "result", "questions", "error", "is_error",
    "human_response",
)


def _build_fe_plan_snapshot(plan):
    """Reduce the full plan dict to the lean shape the FE actually renders.

    Drops server-internal noise: `history`, `history_summary`,
    `message_to_user`, agent routing config, execution_details, timing,
    retry counters, org/bridge/thread identifiers, top-level wrapper
    questions, timestamps. Returns:

      { "state": ..., "goal": ..., "tasks": [ { id, title, task_description,
        dependencies, status, result, questions, error, is_error,
        human_response } ] }
    """
    if not isinstance(plan, dict):
        return plan

    inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
    raw_tasks = inner.get("tasks") if isinstance(inner, dict) else None

    def _scrub_task(task):
        if not isinstance(task, dict):
            return task
        return {k: task.get(k) for k in _FE_TASK_FIELDS if k in task}

    if isinstance(raw_tasks, list):
        tasks = [_scrub_task(t) for t in raw_tasks]
    elif isinstance(raw_tasks, dict):
        tasks = [_scrub_task(v) for v in raw_tasks.values()]
    else:
        tasks = []

    return {
        "state": plan.get("state"),
        "goal": plan.get("goal") or (inner.get("goal") if isinstance(inner, dict) else None),
        "tasks": tasks,
    }


def _format_plan_response(plan, message_id, model="", finish_reason="completed", synthesized=None, include_plan=False):
    """Build the ai_middleware_format `done.accumulated_data` payload.

    `content` always ships a lean FE-shaped plan snapshot ({state, goal,
    tasks: [{id, title, task_description, dependencies, status, result,
    questions, error, is_error, human_response}]}) — same convention as
    the planner's done event but with all server-internal noise (history,
    history_summary, message_to_user, assigned_agent routing, execution
    details, timestamps, identifiers) stripped. When a synthesized answer
    is available it rides alongside in `data.synthesized` so the FE can
    surface it without re-walking the plan tree. `include_plan` is kept
    for backwards-compatibility but no longer changes behavior: the plan
    is always included."""
    sanitized = _build_fe_plan_snapshot(plan) if isinstance(plan, dict) else plan
    content = sanitized if isinstance(sanitized, str) else json.dumps(sanitized)

    synthesized_str = None
    if synthesized:
        synthesized_str = synthesized if isinstance(synthesized, str) else json.dumps(synthesized)

    return {
        "data": {
            "id": message_id,
            "content": content,
            "synthesized": synthesized_str,
            "model": model,
            "role": "assistant",
            "tools_data": {},
            "images": None,
            "annotations": None,
            "fallback": False,
            "firstAttemptError": "",
            "finish_reason": finish_reason,
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0,
        },
    }


def _derive_done_finish_reason(plan):
    """Map plan state to done finish_reason for accurate client semantics."""
    state = (plan or {}).get("state")
    if state == "completed":
        return "stop"
    if state == "paused":
        return "paused"
    if state == "failed":
        return "error"
    return "stop"


async def _stream_plan_action(streamer, action, parsed_data, bridge_configurations, existing_plan):
    """Background task: execute the plan action and emit SSE events."""
    org_id = parsed_data["org_id"]
    bridge_id = parsed_data["bridge_id"]
    thread_id = parsed_data.get("thread_id")
    sub_thread_id = parsed_data.get("sub_thread_id") or thread_id
    message_id = parsed_data.get("message_id", "")
    model = parsed_data.get("model", "")

    try:
        await streamer.emit_start(
            model=model,
            service=parsed_data.get("service", ""),
            bridge_id=bridge_id,
            message_id=message_id,
        )

        if not existing_plan and action not in (None, ""):
            await streamer.emit_error(f"No plan found for action: {action}")
            return

        if action == "approve":
            # Reset previously failed tasks so they retry
            tasks = _get_tasks(existing_plan)
            for task in tasks.values():
                if task["status"] == "failed":
                    task["status"] = "pending"
                    task["retry"] = 0
                    task["error"] = None
            _set_tasks(existing_plan, tasks)
            existing_plan["state"] = "approved"
            await plan_store.update_plan(existing_plan)
            await streamer.emit_delta(json.dumps({"event": "execution_started", "state": "executing"}))
            await streamer.emit_execution()
            # Run executor — stream stays open, task events emitted per task
            main_agent_metrics = await execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan and (final_plan.get("state") == "completed"):
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                synthesized=synthesized or None,
            )
            await streamer.emit_done(
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                message_id=message_id,
                finish_reason=final_finish_reason,
                accumulated_data=formatted,
            )
            # Update the history entry that was created during planning, enriched
            # with aggregated main-agent telemetry (tokens, latency, tools, reasoning).
            asyncio.create_task(
                publish_plan_history_update(
                    parsed_data=parsed_data,
                    final_plan=final_plan,
                    main_agent_metrics=main_agent_metrics,
                    history_params_extra={
                        "message": synthesized or _extract_last_completed_result(final_plan),
                        "finish_reason": final_finish_reason,
                        "status": (final_plan or {}).get("state") == "completed",
                    },
                )
            )

        elif action == "status":
            status_finish_reason = _derive_done_finish_reason(existing_plan)
            await streamer.emit_done(
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                message_id=message_id,
                finish_reason=status_finish_reason,
                accumulated_data=_format_plan_response(
                    existing_plan,
                    message_id,
                    model,
                    finish_reason=status_finish_reason,
                    include_plan=True,
                ),
            )

        elif action == "respond":
            task_id = parsed_data.get("task_id")
            if not task_id:
                await streamer.emit_error("task_id is required for respond action")
                return

            result = await resume_task_handler(
                org_id, bridge_id, thread_id, sub_thread_id,
                task_id, parsed_data.get("user", ""),
            )

            if not result.get("success"):
                await streamer.emit_error(result.get("error", "Failed to resume task"))
                return

            plan = result["plan"]

            # Signal the client we are entering execution mode
            await streamer.emit_execution()
            await streamer.emit_delta(json.dumps({"event": "execution_started", "state": "executing"}))

            # Replay settled tasks so the client can restore its state
            plan_tasks = _get_tasks(plan)
            sorted_task_ids = sorted(
                plan_tasks.keys(),
                key=lambda x: int(x.split("_")[1]) if "_" in x and x.split("_")[1].isdigit() else 0,
            )
            for t_id in sorted_task_ids:
                t = plan_tasks[t_id]
                status = t.get("status")
                if status == "completed":
                    await streamer.emit_delta(json.dumps({"event": "task_started", "task_id": t_id, "title": t.get("title", ""), "replayed": True}))
                    await streamer.emit_delta(json.dumps({"event": "task_completed", "task_id": t_id, "title": t.get("title", ""), "result": t.get("result"), "replayed": True}))
                elif status == "failed":
                    await streamer.emit_delta(json.dumps({"event": "task_started", "task_id": t_id, "title": t.get("title", ""), "replayed": True}))
                    await streamer.emit_delta(json.dumps({"event": "task_error", "task_id": t_id, "title": t.get("title", ""), "is_error": True, "error": t.get("error"), "replayed": True}))

            # Resume execution — live events are streamed through the same connection
            plan["state"] = "approved"
            await plan_store.update_plan(plan)
            main_agent_metrics = await execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan and (final_plan.get("state") == "completed"):
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                synthesized=synthesized or None,
            )
            await streamer.emit_done(
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                message_id=message_id,
                finish_reason=final_finish_reason,
                accumulated_data=formatted,
            )
            asyncio.create_task(
                publish_plan_history_update(
                    parsed_data=parsed_data,
                    final_plan=final_plan,
                    main_agent_metrics=main_agent_metrics,
                    history_params_extra={
                        "message": synthesized or _extract_last_completed_result(final_plan),
                        "finish_reason": final_finish_reason,
                        "status": (final_plan or {}).get("state") == "completed",
                    },
                )
            )

        elif action == "retry":
            task_id = parsed_data.get("task_id")
            if not task_id:
                await streamer.emit_error("task_id is required for retry action")
                return
            
            # Reset the specific task to pending so it will be re-executed
            tasks = _get_tasks(existing_plan)
            task = tasks.get(task_id)
            if not task:
                await streamer.emit_error(f"Task {task_id} not found")
                return

            def _reset_task(t):
                t["status"] = "pending"
                t["retry"] = 0
                t["result"] = None
                t["error"] = None
                t["is_error"] = False

            _reset_task(task)
            # Also reset downstream tasks that failed because this task failed
            for t in tasks.values():
                if task_id in t.get("dependencies", []) and t.get("status") == "failed":
                    _reset_task(t)
            _set_tasks(existing_plan, tasks)
            existing_plan["state"] = "approved"
            await plan_store.update_plan(existing_plan)
            
            # Emit execution event and restart executor
            await streamer.emit_delta(json.dumps({"event": "execution_started", "state": "executing"}))
            await streamer.emit_execution()
            main_agent_metrics = await execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan and (final_plan.get("state") == "completed"):
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                synthesized=synthesized or None,
            )
            await streamer.emit_done(
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                message_id=message_id,
                finish_reason=final_finish_reason,
                accumulated_data=formatted,
            )
            asyncio.create_task(
                publish_plan_history_update(
                    parsed_data=parsed_data,
                    final_plan=final_plan,
                    main_agent_metrics=main_agent_metrics,
                    history_params_extra={
                        "message": synthesized or _extract_last_completed_result(final_plan),
                        "finish_reason": final_finish_reason,
                        "status": (final_plan or {}).get("state") == "completed",
                    },
                )
            )

        elif action == "cancel":
            existing_plan["state"] = "failed"
            await plan_store.update_plan(existing_plan)
            await streamer.emit_done(
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                message_id=message_id,
                finish_reason="stop",
                accumulated_data=_format_plan_response(
                    existing_plan, message_id, model, finish_reason="cancelled",
                ),
            )

    except Exception as e:
        logger.error(f"Error in plan streaming: {e}")
        await streamer.emit_error(str(e))
    finally:
        await streamer.close()


async def handle_todo_mode(parsed_data, bridge_configurations):
    """
    Main dispatcher for plan mode. Always returns an SSE StreamingResponse.
    - create/update: LLM tokens stream live, done.response matches ai_middleware_format
    - approve: stream stays open through execution, task events emitted per task
    - status/cancel/respond: immediate result in done.response
    """
    thread_id = parsed_data.get("thread_id")
    if not thread_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "thread_id is required for plan mode"},
        )

    org_id = parsed_data["org_id"]
    bridge_id = parsed_data["bridge_id"]
    sub_thread_id = parsed_data.get("sub_thread_id") or thread_id
    action = parsed_data.get("action")

    existing_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)

    streamer = StreamingService(mode="sse")
    asyncio.create_task(
        _stream_plan_action(streamer, action, parsed_data, bridge_configurations, existing_plan)
    )

    return StreamingResponse(streamer.generator(), media_type="text/event-stream")
