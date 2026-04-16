import asyncio
import copy
import json
import uuid

from globals import logger
from src.services.todo import plan_store


SUCCESS_TERMINAL_STATUSES = {"completed", "skipped"}
BLOCKING_TERMINAL_STATUSES = {"failed", "skipped"}
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cost")


def _get_runnable_tasks(tasks):
    """Find tasks that are pending and have all dependencies completed."""
    runnable = []
    for task_id, task in tasks.items():
        if task["status"] != "pending":
            continue
        deps = task.get("dependencies", [])
        all_deps_met = all(
            tasks.get(dep, {}).get("status") == "completed" for dep in deps
        )
        if all_deps_met:
            runnable.append(task_id)
    return runnable


def _has_in_progress_tasks(tasks):
    return any(task.get("status") == "in_progress" for task in tasks.values())


def _is_plan_complete(tasks):
    """Plan is complete only when every task finished successfully or was skipped."""
    return all(task.get("status") in SUCCESS_TERMINAL_STATUSES for task in tasks.values())


def _is_plan_paused(tasks):
    """Plan is paused only when human input is required and nothing else can run."""
    return (
        any(task.get("status") == "waiting_for_user" for task in tasks.values())
        and not _has_in_progress_tasks(tasks)
        and not _get_runnable_tasks(tasks)
    )


def _is_plan_failed(tasks):
    """Plan is failed when a task failed and there is no remaining runnable work."""
    return (
        any(task.get("status") == "failed" for task in tasks.values())
        and not _has_in_progress_tasks(tasks)
        and not _get_runnable_tasks(tasks)
    )


def _normalize_usage(usage):
    usage = usage or {}
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens) or 0
    cost = usage.get("cost", usage.get("expectedCost", 0)) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost": cost,
    }


def _merge_usage(current_usage, additional_usage):
    current_usage = _normalize_usage(current_usage)
    additional_usage = _normalize_usage(additional_usage)
    return {
        field: current_usage.get(field, 0) + additional_usage.get(field, 0)
        for field in USAGE_FIELDS
    }


def _get_usage_summary(plan):
    usage_summary = plan.setdefault("usage_summary", {})
    usage_summary.setdefault("planner", _normalize_usage({}))
    usage_summary.setdefault("execution", _normalize_usage({}))
    usage_summary.setdefault("tasks", {})
    usage_summary.setdefault("bridges", {})
    usage_summary.setdefault("totals", _normalize_usage({}))
    return usage_summary


def _record_task_usage(plan, task_id, assigned_agent, usage):
    normalized_usage = _normalize_usage(usage)
    if not any(normalized_usage.values()):
        return

    usage_summary = _get_usage_summary(plan)

    merged_task_usage = _merge_usage(usage_summary["tasks"].get(task_id, {}), normalized_usage)
    usage_summary["tasks"][task_id] = merged_task_usage
    usage_summary["bridges"][assigned_agent] = _merge_usage(
        usage_summary["bridges"].get(assigned_agent, {}),
        normalized_usage,
    )
    usage_summary["execution"] = _merge_usage(usage_summary["execution"], normalized_usage)
    usage_summary["totals"] = _merge_usage(usage_summary["totals"], normalized_usage)

    task = plan.get("tasks", {}).get(task_id)
    if task is not None:
        task["usage"] = merged_task_usage


def _skip_tasks_blocked_by_failed_dependencies(tasks):
    """Propagate dependency failures so plans do not get stuck in a false paused state."""
    changed = False

    for task_id, task in tasks.items():
        if task.get("status") not in {"pending", "waiting_for_user"}:
            continue

        deps = task.get("dependencies", [])
        blocking_dep = next(
            (
                dep for dep in deps
                if tasks.get(dep, {}).get("status") in BLOCKING_TERMINAL_STATUSES
            ),
            None,
        )
        if not blocking_dep:
            continue

        blocked_status = tasks.get(blocking_dep, {}).get("status")
        task["status"] = "skipped"
        task["is_error"] = True
        task["error"] = f"Blocked by dependency {blocking_dep} ({blocked_status})"
        task["result"] = None
        changed = True

    return changed


def reset_tasks_for_reexecution(tasks, root_task_ids=None):
    """
    Reset failed tasks and their blocked descendants so execution can resume cleanly.
    """
    root_task_ids = set(root_task_ids or [])
    if not root_task_ids:
        root_task_ids = {
            task_id for task_id, task in tasks.items() if task.get("status") == "failed"
        }

    for task_id in root_task_ids:
        task = tasks.get(task_id)
        if not task:
            continue
        task["status"] = "pending"
        task["retry"] = 0
        task["result"] = None
        task["error"] = None
        task["is_error"] = False

    changed = True
    while changed:
        changed = False
        for task in tasks.values():
            if task.get("status") != "skipped":
                continue
            if not str(task.get("error") or "").startswith("Blocked by dependency"):
                continue

            deps = task.get("dependencies", [])
            if any(tasks.get(dep, {}).get("status") in BLOCKING_TERMINAL_STATUSES for dep in deps):
                continue

            task["status"] = "pending"
            task["retry"] = 0
            task["result"] = None
            task["error"] = None
            task["is_error"] = False
            changed = True

    return root_task_ids


async def _execute_single_task(task_id, task, org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, plan, streamer=None):
    """Execute a single task by calling the appropriate agent directly.
    
    When `streamer` is provided, delta/reasoning/tool events from the agent's
    stream are forwarded to the client in real-time, tagged with `task_id`.
    """
    assigned_agent = task.get("assigned_agent") or bridge_id

    task_description = task.get("task_description", task.get("title", ""))
    human_response = task.get("human_response")
    if human_response:
        task_description = f"{task_description}\n\nHuman Response: {human_response}"

    try:
        from src.services.commonServices.common import chat_multiple_agents

        if assigned_agent in bridge_configurations:
            resolved_bridge_configurations = copy.deepcopy(bridge_configurations)
        else:
            logger.warning(
                "Assigned agent %s not found in pre-fetched bridge_configurations; "
                "falling back to getConfiguration during plan execution.",
                assigned_agent,
            )
            from src.services.utils.getConfiguration import getConfiguration

            resolved_config = await getConfiguration(
                configuration=None,
                service=None,
                bridge_id=assigned_agent,
                apikey=None,
                variables={},
                org_id=org_id,
                version_id=None,
                override_fields={},
            )
            if not resolved_config.get("success"):
                return {"success": False, "error": resolved_config.get("error", "Failed to resolve agent configuration")}
            resolved_bridge_configurations = resolved_config.get("bridge_configurations", {})

        request_body = {
            "user": task_description,
            "bridge_id": assigned_agent,
            "message_id": str(uuid.uuid1()),
            "thread_id": thread_id,
            "sub_thread_id": sub_thread_id,
            "org_id": org_id,
            "variables": {},
            "bridge_configurations": resolved_bridge_configurations,
            "plan_execution": True,
            # Plan execution keeps a single canonical history row on the top-level
            # plan message, so all sub-task history is suppressed.
            "skip_history": True,
        }

        # Match the direct request path by going through chat_multiple_agents,
        # which applies the DB-backed agent config before entering chat().
        if streamer:
            request_body.setdefault("configuration", {})["stream"] = True

        data_to_send = {"body": request_body, "state": {}}
        response = await chat_multiple_agents(data_to_send)
        
        if hasattr(response, "body"):
            response_data = json.loads(response.body.decode("utf-8"))
            if response_data.get("success"):
                response_payload = response_data.get("response", {}) or {}
                content = response_payload.get("data", {}).get("content", "")
                return {
                    "success": True,
                    "result": content,
                    "usage": response_payload.get("usage", {}),
                    "assigned_agent": assigned_agent,
                }
            else:
                return {
                    "success": False,
                    "error": response_data.get("error") or response_data.get("message") or "Task execution failed",
                    "assigned_agent": assigned_agent,
                }

        elif hasattr(response, "body_iterator"):
            accumulated_content = []
            done_event = None
            final_usage = {}
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8")
                for line in chunk.split("\n"):
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    evt_type = event.get("event")

                    if evt_type == "delta":
                        content_piece = event.get("content", "")
                        accumulated_content.append(content_piece)
                        if streamer:
                            await streamer.emit_task_delta(task_id, content_piece)

                    elif evt_type == "reasoning":
                        if streamer:
                            await streamer.emit_task_reasoning(task_id, event.get("content", ""))

                    elif evt_type == "tool_call":
                        if streamer:
                            await streamer.emit_task_tool_call(
                                task_id,
                                name=event.get("name", ""),
                                args=event.get("args", {}),
                                call_id=event.get("call_id", ""),
                            )

                    elif evt_type == "tool_result":
                        if streamer:
                            await streamer.emit_task_tool_result(
                                task_id,
                                name=event.get("name", ""),
                                content=event.get("content", ""),
                                call_id=event.get("call_id", ""),
                            )

                    elif evt_type == "done":
                        done_event = event
                        final_usage = event.get("usage", {}) or event.get("response", {}).get("usage", {})
            
            content = "".join(accumulated_content)
            if done_event and done_event.get("response", {}).get("data", {}).get("content"):
                content = done_event["response"]["data"]["content"]
            
            return {
                "success": True,
                "result": content,
                "usage": final_usage,
                "assigned_agent": assigned_agent,
            }
        else:
            if response.get("success"):
                response_payload = response.get("response", {}) or {}
                content = response_payload.get("data", {}).get("content", "")
                return {
                    "success": True,
                    "result": content,
                    "usage": response_payload.get("usage", {}),
                    "assigned_agent": assigned_agent,
                }
            else:
                return {
                    "success": False,
                    "error": response.get("error") or response.get("message") or "Task execution failed",
                    "assigned_agent": assigned_agent,
                }

    except Exception as e:
        logger.error(f"Error executing task {task_id}: {e}")
        return {"success": False, "error": str(e), "assigned_agent": assigned_agent}


async def execute_plan(org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, streamer=None):
    """
    Execute all tasks respecting dependencies and parallelism.
    If `streamer` is provided, task progress events are emitted live via SSE.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        logger.error(f"Plan not found for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
        return

    plan["state"] = "executing"
    await plan_store.update_plan(plan)

    async def _emit(event_type, data):
        if streamer:
            await streamer.emit_delta(json.dumps({"event": event_type, **data}))

    while True:
        # Refresh plan from store (in case of external updates like HIL responses)
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if not plan:
            break
        tasks = plan.get("tasks", {})

        if _skip_tasks_blocked_by_failed_dependencies(tasks):
            await plan_store.update_plan(plan)
            tasks = plan.get("tasks", {})

        if _is_plan_failed(tasks):
            plan["state"] = "failed"
            await plan_store.update_plan(plan)
            logger.info(f"Plan failed for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            await _emit("plan_completed", {"state": "failed", "plan": plan})
            break

        if _is_plan_complete(tasks):
            plan["state"] = "completed"
            await plan_store.update_plan(plan)
            logger.info(f"Plan completed for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            await _emit("plan_completed", {"state": "completed", "plan": plan})
            break

        if _is_plan_paused(tasks):
            plan["state"] = "paused"
            await plan_store.update_plan(plan)
            logger.info(f"Plan paused (waiting for user) for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            await _emit("plan_paused", {"state": "paused", "plan": plan})
            break

        # Find runnable tasks
        runnable = _get_runnable_tasks(tasks)
        if not runnable:
            await asyncio.sleep(1)
            continue

        # Mark runnable tasks as in_progress and notify
        for task_id in runnable:
            tasks[task_id]["status"] = "in_progress"
            await _emit("task_started", {"task_id": task_id, "title": tasks[task_id].get("title", "")})
        await plan_store.update_plan(plan)

        # Execute runnable tasks in parallel (streamer forwarded for live events)
        coroutines = [
            _execute_single_task(
                task_id, tasks[task_id],
                org_id, bridge_id, thread_id, sub_thread_id,
                bridge_configurations, plan, streamer=streamer,
            )
            for task_id in runnable
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # Process results
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        tasks = plan.get("tasks", {})

        for task_id, result in zip(runnable, results):
            task = tasks[task_id]

            if isinstance(result, Exception):
                result = {"success": False, "error": str(result)}

            _record_task_usage(
                plan,
                task_id,
                result.get("assigned_agent") or task.get("assigned_agent") or bridge_id,
                result.get("usage", {}),
            )

            if result["success"]:
                task["status"] = "completed"
                task["is_error"] = False
                task["error"] = None
                task["result"] = result.get("result")
                await _emit("task_completed", {"task_id": task_id, "title": task.get("title", ""), "result": task["result"]})
            else:
                task["retry"] = task.get("retry", 0) + 1
                task["is_error"] = True
                task["error"] = result.get("error")
                max_retry = task.get("max_retry", 2)
                if task["retry"] < max_retry:
                    task["status"] = "pending"
                    logger.info(f"Task {task_id} failed, retry {task['retry']}/{max_retry}")
                    await _emit("task_error", {
                        "task_id": task_id,
                        "title": task.get("title", ""),
                        "is_error": True,
                        "error": task["error"],
                        "retry": task["retry"],
                        "max_retry": max_retry,
                        "retrying": True,
                    })
                else:
                    task["status"] = "failed"
                    logger.error(f"Task {task_id} failed after {max_retry} retries: {task['error']}")
                    await _emit("task_error", {
                        "task_id": task_id,
                        "title": task.get("title", ""),
                        "is_error": True,
                        "error": task["error"],
                        "retry": task["retry"],
                        "max_retry": max_retry,
                        "retrying": False,
                    })

        _skip_tasks_blocked_by_failed_dependencies(tasks)
        await plan_store.update_plan(plan)

    # Final state check
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if plan and plan["state"] == "executing":
        tasks = plan.get("tasks", {})
        if _skip_tasks_blocked_by_failed_dependencies(tasks):
            await plan_store.update_plan(plan)
            tasks = plan.get("tasks", {})

        if _is_plan_failed(tasks):
            plan["state"] = "failed"
        elif _is_plan_paused(tasks):
            plan["state"] = "paused"
        elif _is_plan_complete(tasks):
            plan["state"] = "completed"

        await plan_store.update_plan(plan)
        if plan["state"] == "paused":
            await _emit("plan_paused", {"state": plan["state"], "plan": plan})
        else:
            await _emit("plan_completed", {"state": plan["state"], "plan": plan})


async def resume_task(org_id, bridge_id, thread_id, sub_thread_id, task_id, human_response):
    """
    Store human response and reset the task to pending.
    The caller is responsible for driving execute_plan so that execution
    events are streamed back to the client.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return {"success": False, "error": "Plan not found"}

    task = plan.get("tasks", {}).get(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    if task["status"] != "waiting_for_user":
        return {"success": False, "error": f"Task {task_id} is not waiting for user input (status: {task['status']})"}

    task["human_response"] = human_response
    task["status"] = "pending"
    task["retry"] = 0
    task["result"] = None
    task["error"] = None
    task["is_error"] = False
    await plan_store.update_plan(plan)

    return {"success": True, "plan": plan}
