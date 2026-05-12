import asyncio
import json

from fastapi.responses import JSONResponse, StreamingResponse

from globals import logger
from src.services.commonServices.streaming_service import StreamingService
from src.services.todo import executor_service, plan_store
from src.services.todo.plan_store import _get_tasks, _set_tasks
from src.services.todo import synthesizer_service
from src.db_services.metrics_service import publish_plan_history_update


def _format_plan_response(plan, message_id, model="", finish_reason="completed", extract_final_result=False):
    """
    Wrap plan in the standard ai_middleware_format.py response shape.
    
    Args:
        plan: The plan dict with tasks
        message_id: Message ID
        model: Model name
        finish_reason: Reason for completion
        extract_final_result: If True and plan is completed, extract only the last task's result
    """
    content = plan
    
    # If plan is completed and extract_final_result is True, get the last task's result
    if extract_final_result and plan.get("state") == "completed":
        tasks = _get_tasks(plan)
        if tasks:
            # Find the last completed task (highest task number)
            completed_tasks = {
                task_id: task for task_id, task in tasks.items() 
                if task.get("status") == "completed"
            }
            if completed_tasks:
                # Sort by task_id (assuming task_1, task_2, etc.) to get the last one
                sorted_task_ids = sorted(completed_tasks.keys(), key=lambda x: int(x.split("_")[1]) if "_" in x else 0)
                last_task_id = sorted_task_ids[-1]
                last_task = completed_tasks[last_task_id]
                
                # Extract the result - handle both string and dict formats
                result = last_task.get("result", "")
                if isinstance(result, str):
                    try:
                        # Try to parse if it's a JSON string with a "data" field
                        parsed_result = json.loads(result)
                        if isinstance(parsed_result, dict) and "data" in parsed_result:
                            content = parsed_result["data"]
                        else:
                            content = result
                    except (json.JSONDecodeError, ValueError):
                        # If not valid JSON, use as-is
                        content = result
                else:
                    content = result
    
    # Convert content to JSON string if it's not already a string
    if not isinstance(content, str):
        content = json.dumps(content)
    
    return {
        "data": {
            "id": message_id,
            "content": content,
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
            main_agent_metrics = await executor_service.execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan:
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                extract_final_result=not synthesized,
            )
            if synthesized:
                formatted["data"]["content"] = synthesized
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
                        "message": formatted["data"]["content"],
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
                ),
            )

        elif action == "respond":
            task_id = parsed_data.get("task_id")
            if not task_id:
                await streamer.emit_error("task_id is required for respond action")
                return

            result = await executor_service.resume_task(
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
            main_agent_metrics = await executor_service.execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan:
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                extract_final_result=not synthesized,
            )
            if synthesized:
                formatted["data"]["content"] = synthesized
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
                        "message": formatted["data"]["content"],
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

            task["status"] = "pending"
            task["retry"] = 0
            task["result"] = None
            task["error"] = None
            task["is_error"] = False
            _set_tasks(existing_plan, tasks)
            existing_plan["state"] = "approved"
            await plan_store.update_plan(existing_plan)
            
            # Emit execution event and restart executor
            await streamer.emit_delta(json.dumps({"event": "execution_started", "state": "executing"}))
            await streamer.emit_execution()
            main_agent_metrics = await executor_service.execute_plan(
                org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=streamer
            )
            final_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
            final_finish_reason = _derive_done_finish_reason(final_plan)
            synthesized = ""
            if final_plan:
                synthesized = await synthesizer_service.synthesize_results(
                    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=streamer,
                )
            formatted = _format_plan_response(
                final_plan,
                message_id,
                model,
                finish_reason=final_finish_reason,
                extract_final_result=not synthesized,
            )
            if synthesized:
                formatted["data"]["content"] = synthesized
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
                        "message": formatted["data"]["content"],
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
                accumulated_data=_format_plan_response(existing_plan, message_id, model, finish_reason="cancelled"),
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
