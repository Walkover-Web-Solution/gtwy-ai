import asyncio
import copy
import json
import time
import uuid

from globals import logger
from src.services.todo import plan_store


TERMINAL_STATUSES = {"completed", "failed", "skipped", "waiting_for_user"}


def _init_main_agent_metrics():
    """
    Aggregate container for main-agent task telemetry. Primary-agent sub-tasks
    run with skip_history=True, so nothing else persists their tokens/latency/
    tools/reasoning. We sum them here and hand them to the final history update.
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "expected_cost": 0.0,
        "latency_total": 0.0,
        "per_task_latency": [],
        "tools_call_data": [],
        "_tool_calls_by_id": {},
        "reasoning_parts": [],
        "service": None,
        "model": None,
        "finish_reason": "stop",
        "success": True,
        "firstAttemptError": None,
        "AiConfig": None,
        "fallback_model": None,
        "llm_urls": [],
        "annotations": [],
        "last_error": None,
    }


def _merge_task_metrics(
    metrics,
    task_id,
    done_event,
    collected_tool_calls,
    reasoning_buf,
    task_success,
    error,
    *,
    agent_config=None,
    elapsed_seconds=None,
    fallback_model=None,
    service=None,
    model=None,
):
    """Fold a single primary-agent task's telemetry into the shared aggregate.

    `agent_config`, `fallback_model`, `service`, `model`, `elapsed_seconds`
    come from the executor context (bridge_configurations + wall-clock timing)
    because the inner chat's SSE `done` event only carries `response.data`
    and `response.usage`.  AiConfig / fallback_model / latency / firstAttemptError
    live in `result.historyParams` inside baseService and are never streamed.
    """
    if metrics is None:
        return

    usage = (done_event or {}).get("usage") or {}
    response = (done_event or {}).get("response") or {}
    data = response.get("data") or {}

    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    total_t = usage.get("total_tokens") or (input_t + output_t)
    cost = usage.get("cost") or usage.get("expected_cost") or 0

    metrics["input_tokens"] += input_t
    metrics["output_tokens"] += output_t
    metrics["total_tokens"] += total_t
    try:
        metrics["expected_cost"] += float(cost or 0)
    except (TypeError, ValueError):
        pass

    # Latency: prefer the provider-reported figure, fall back to wall-clock.
    provider_latency = data.get("latency")
    if isinstance(provider_latency, dict):
        over_all = provider_latency.get("over_all_time") or 0
    elif isinstance(provider_latency, (int, float)):
        over_all = provider_latency
    else:
        over_all = 0
    try:
        over_all = float(over_all or 0)
    except (TypeError, ValueError):
        over_all = 0.0
    if over_all <= 0 and elapsed_seconds is not None:
        try:
            over_all = float(elapsed_seconds)
        except (TypeError, ValueError):
            over_all = 0.0
    metrics["latency_total"] += over_all
    metrics["per_task_latency"].append({"task_id": task_id, "time": round(over_all, 4)})

    effective_model = data.get("model") or model
    if effective_model and not metrics["model"]:
        metrics["model"] = effective_model
    effective_service = response.get("service") or service
    if effective_service and not metrics["service"]:
        metrics["service"] = effective_service

    if response.get("firstAttemptError"):
        metrics["firstAttemptError"] = response["firstAttemptError"]

    # AiConfig = the bridge's customConfig sent to the LLM.  Pulled from the
    # bridge configuration we already have in hand.
    if agent_config and not metrics["AiConfig"]:
        metrics["AiConfig"] = agent_config

    if fallback_model and not metrics["fallback_model"]:
        metrics["fallback_model"] = fallback_model

    if response.get("llm_urls"):
        metrics["llm_urls"].extend(response["llm_urls"])
    if response.get("annotations"):
        metrics["annotations"].extend(response["annotations"])

    if reasoning_buf:
        metrics["reasoning_parts"].append("".join(reasoning_buf))

    if collected_tool_calls:
        metrics["tools_call_data"].extend(collected_tool_calls)

    if not task_success:
        metrics["success"] = False
        metrics["finish_reason"] = "error"
        if error:
            metrics["last_error"] = str(error)


def finalize_main_agent_metrics(metrics):
    """Shape the aggregate into the fields needed by the history payload."""
    if not metrics:
        return None
    return {
        "input_tokens": metrics["input_tokens"],
        "output_tokens": metrics["output_tokens"],
        "total_tokens": metrics["total_tokens"],
        "expected_cost": metrics["expected_cost"],
        "latency": {
            "over_all_time": metrics["latency_total"],
            "per_task": metrics["per_task_latency"],
        },
        "tools_call_data": metrics["tools_call_data"],
        "reasoning": "\n".join(p for p in metrics["reasoning_parts"] if p),
        "service": metrics["service"],
        "model": metrics["model"],
        "finish_reason": metrics["finish_reason"],
        "success": metrics["success"],
        "firstAttemptError": metrics["firstAttemptError"],
        "AiConfig": metrics["AiConfig"],
        "fallback_model": metrics["fallback_model"] or {},
        "llm_urls": metrics["llm_urls"],
        "annotations": metrics["annotations"],
        "last_error": metrics["last_error"],
    }


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


def _is_plan_blocked(tasks):
    """Check if the plan is blocked - no runnable tasks and not all done."""
    for task_id, task in tasks.items():
        if task["status"] == "pending":
            deps = task.get("dependencies", [])
            all_deps_met = all(
                tasks.get(dep, {}).get("status") == "completed" for dep in deps
            )
            if all_deps_met:
                return False  # At least one task can still run
    # Check if any tasks are still in progress
    for task in tasks.values():
        if task["status"] == "in_progress":
            return False  # Still executing
    # All pending tasks have unmet deps, and nothing is in progress
    has_pending = any(t["status"] == "pending" for t in tasks.values())
    return has_pending


def _is_plan_complete(tasks):
    """Check if all tasks are in a terminal state."""
    return all(t["status"] in TERMINAL_STATUSES for t in tasks.values())


def _inject_variables_into_tool_args(tool_name, args, variables, variables_path, tool_id_and_name_mapping):
    """Inject static variables into tool arguments based on variables_path mapping.
    Matches the behavior of replace_variables_in_args in main flow.
    """
    if not variables_path or not variables:
        return args
    
    import pydash as _
    
    # Get the function name for variable path lookup
    tool_mapping = tool_id_and_name_mapping.get(tool_name, {})
    if tool_mapping.get("type") == "AGENT":
        function_name = tool_mapping.get("bridge_id", "")
    else:
        function_name = tool_mapping.get("name", tool_name)
    
    # Inject variables based on variables_path mapping
    enriched_args = dict(args or {})
    function_variables_path = variables_path.get(function_name, {})
    
    for path_key, path_value in function_variables_path.items():
        value_to_set = _.objects.get(variables, path_value)
        if value_to_set is not None:
            _.objects.set_(enriched_args, path_key, value_to_set)
    
    return enriched_args


async def _execute_single_task(task_id, task, org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, plan, streamer=None, main_agent_metrics=None, variables=None, variables_path=None):
    """Execute a single task by calling the appropriate agent directly.
    
    When `streamer` is provided, delta/reasoning/tool events from the agent's
    stream are forwarded to the client in real-time, tagged with `task_id`.

    When `main_agent_metrics` is provided and the task runs on the main bridge,
    per-task tokens / latency / reasoning / tool_calls are folded into the
    aggregate so the final history update can persist them.
    """
    assigned_agent = task.get("assigned_agent") or bridge_id
    # A task is a "primary-agent sub-task" when no explicit agent was assigned
    # (or the assigned agent is the main bridge itself).  For these we skip
    # per-sub-task history so the conversation log shows only the final plan
    # result saved by todo_handler, not every intermediate LLM call.
    is_primary_agent_task = not task.get("assigned_agent") or task.get("assigned_agent") == bridge_id
    aggregate_metrics = main_agent_metrics if is_primary_agent_task else None

    task_description = task.get("task_description", task.get("title", ""))
    human_response = task.get("human_response")
    if human_response:
        task_description = f"{task_description}\n\nHuman Response: {human_response}"

    task_started_at = time.perf_counter() if aggregate_metrics is not None else None

    try:
        from src.services.commonServices.common import chat_multiple_agents
        from src.services.utils.getConfiguration import getConfiguration

        current_agent_config = bridge_configurations.get(assigned_agent, {})
        resolved_config = await getConfiguration(
            configuration=None,
            service=None,
            bridge_id=assigned_agent,
            apikey=None,
            variables=variables or {},
            org_id=org_id,
            version_id=current_agent_config.get("version_id"),
            override_fields={},
        )
        if not resolved_config.get("success"):
            return {"success": False, "error": resolved_config.get("error", "Failed to resolve agent configuration")}

        request_body = {
            "user": task_description,
            "bridge_id": assigned_agent,
            "message_id": str(uuid.uuid1()),
            "thread_id": thread_id,
            "sub_thread_id": sub_thread_id,
            "org_id": org_id,
            "variables": variables or {},
            "variables_path": variables_path or {},
            "bridge_configurations": copy.deepcopy(resolved_config.get("bridge_configurations", {})),
            "plans": plan,
            # Skip per-sub-task history for the primary agent; its final
            # response is saved once by todo_handler after full execution.
            "skip_history": is_primary_agent_task,
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
                content = response_data.get("response", {}).get("data", {}).get("content", "")
                return {"success": True, "result": content}
            else:
                return {"success": False, "error": response_data.get("error") or response_data.get("message") or "Task execution failed"}

        elif hasattr(response, "body_iterator"):
            accumulated_content = []
            done_event = None
            reasoning_parts = []
            tool_calls_by_id = {}
            tool_calls_order = []
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
                        reasoning_piece = event.get("content", "")
                        if aggregate_metrics is not None and reasoning_piece:
                            reasoning_parts.append(reasoning_piece)
                        if streamer:
                            await streamer.emit_task_reasoning(task_id, reasoning_piece)

                    elif evt_type == "tool_call":
                        call_id = event.get("call_id", "")
                        if aggregate_metrics is not None:
                            # Format matches main flow: {call_id: {name, args, data, id}}
                            if not call_id:
                                # Generate fallback ID if missing
                                call_id = f"tool_{task_id}_{len(tool_calls_order)}"
                            
                            if call_id not in tool_calls_by_id:
                                # Inject static variables into args before storing in history
                                # This matches main flow where replace_variables_in_args is called
                                # before process_data_and_run_tools builds tool history
                                raw_args = event.get("args", {})
                                tool_name = event.get("name", "")
                                tool_id_and_name_mapping = (bridge_configurations.get(assigned_agent, {}) or {}).get("tool_id_and_name_mapping", {})
                                enriched_args = _inject_variables_into_tool_args(
                                    tool_name, raw_args, variables, variables_path, tool_id_and_name_mapping
                                )
                                
                                # Create entry matching main flow format
                                tool_entry = {
                                    call_id: {
                                        "name": tool_name,
                                        "args": enriched_args,  # Store enriched args with injected variables
                                        "data": None,  # Will be updated when tool_result arrives
                                        "id": tool_name,  # Tool identifier
                                    }
                                }
                                tool_calls_by_id[call_id] = tool_entry[call_id]
                                tool_calls_order.append(tool_entry)
                        if streamer:
                            await streamer.emit_task_tool_call(
                                task_id,
                                name=event.get("name", ""),
                                args=event.get("args", {}),
                                call_id=call_id,
                            )

                    elif evt_type == "tool_result":
                        call_id = event.get("call_id", "")
                        result_content = event.get("content", "")
                        if aggregate_metrics is not None:
                            if call_id and call_id in tool_calls_by_id:
                                # Update the data field in existing entry
                                tool_calls_by_id[call_id]["data"] = {
                                    "response": result_content,
                                    "status": 1,
                                    "metadata": {"type": "function"}
                                }
                            else:
                                # Tool result without prior tool_call - create standalone entry
                                if not call_id:
                                    call_id = f"tool_{task_id}_{len(tool_calls_order)}"
                                tool_entry = {
                                    call_id: {
                                        "name": event.get("name", ""),
                                        "args": {},
                                        "data": {
                                            "response": result_content,
                                            "status": 1,
                                            "metadata": {"type": "function"}
                                        },
                                        "id": event.get("name", ""),
                                    }
                                }
                                tool_calls_order.append(tool_entry)
                        if streamer:
                            await streamer.emit_task_tool_result(
                                task_id,
                                name=event.get("name", ""),
                                content=result_content,
                                call_id=call_id,
                            )

                    elif evt_type == "done":
                        done_event = event

            content = "".join(accumulated_content)
            if done_event and done_event.get("response", {}).get("data", {}).get("content"):
                content = done_event["response"]["data"]["content"]

            if aggregate_metrics is not None:
                elapsed = (
                    time.perf_counter() - task_started_at
                    if task_started_at is not None
                    else None
                )
                _merge_task_metrics(
                    aggregate_metrics,
                    task_id,
                    done_event,
                    tool_calls_order,
                    reasoning_parts,
                    task_success=True,
                    error=None,
                    agent_config=current_agent_config.get("configuration"),
                    elapsed_seconds=elapsed,
                    fallback_model=current_agent_config.get("fall_back"),
                    service=current_agent_config.get("service"),
                    model=(current_agent_config.get("configuration") or {}).get("model"),
                )

            return {"success": True, "result": content}
        else:
            if response.get("success"):
                content = response.get("response", {}).get("data", {}).get("content", "")
                return {"success": True, "result": content}
            else:
                return {"success": False, "error": response.get("error") or response.get("message") or "Task execution failed"}

    except Exception as e:
        logger.error(f"Error executing task {task_id}: {e}")
        return {"success": False, "error": str(e)}


async def execute_plan(org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=None):
    """
    Execute all tasks respecting dependencies and parallelism.
    If `streamer` is provided, task progress events are emitted live via SSE.

    Returns the finalized main-agent metrics aggregate (tokens, latency,
    reasoning, tools_call_data, etc.) so the caller can attach it to the
    history update. Connected-agent tasks are excluded — they persist their
    own rows via the normal chat path.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        logger.error(f"Plan not found for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
        return None

    main_agent_metrics = _init_main_agent_metrics()

    plan["state"] = "executing"
    await plan_store.update_plan(plan)

    async def _emit(event_type, data):
        if streamer:
            await streamer.emit_delta(json.dumps({"event": event_type, **data}))

    tasks = plan.get("tasks", {})

    while True:
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if not plan:
            return finalize_main_agent_metrics(main_agent_metrics)
        tasks = plan.get("tasks", {})

        # Check if all tasks are done
        if _is_plan_complete(tasks):
            plan["state"] = "completed"
            await plan_store.update_plan(plan)
            logger.info(f"Plan completed for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            await _emit("plan_completed", {"state": "completed", "plan": plan})
            break

        # Check if plan is blocked
        if _is_plan_blocked(tasks):
            plan["state"] = "paused"
            await plan_store.update_plan(plan)
            logger.info(f"Plan paused (blocked) for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
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
        # Extract variables from parsed_data for connected agent calls
        plan_variables = parsed_data.get("variables") or {}
        plan_variables_path = parsed_data.get("variables_path") or {}
        
        coroutines = [
            _execute_single_task(
                task_id, tasks[task_id],
                org_id, bridge_id, thread_id, sub_thread_id,
                bridge_configurations, plan, streamer=streamer,
                main_agent_metrics=main_agent_metrics,
                variables=plan_variables,
                variables_path=plan_variables_path,
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

        await plan_store.update_plan(plan)

    # Final state check
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if plan and plan["state"] == "executing":
        has_failures = any(t["status"] == "failed" for t in plan.get("tasks", {}).values())
        plan["state"] = "failed" if has_failures else "completed"
        await plan_store.update_plan(plan)
        await _emit("plan_completed", {"state": plan["state"], "plan": plan})

    return finalize_main_agent_metrics(main_agent_metrics)


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
    await plan_store.update_plan(plan)

    return {"success": True, "plan": plan}
