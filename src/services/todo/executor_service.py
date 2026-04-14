import asyncio
import json
import uuid

from globals import logger
from src.services.todo import plan_store


TERMINAL_STATUSES = {"completed", "failed", "skipped", "waiting_for_user"}


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


async def _execute_single_task(task_id, task, org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, plan):
    """Execute a single task by calling the appropriate agent directly."""
    assigned_agent = task.get("assigned_agent") or bridge_id
    task_description = task.get("task_description", task.get("title", ""))
    human_response = task.get("human_response")
    if human_response:
        task_description = f"{task_description}\n\nHuman Response: {human_response}"

    try:
        # Import chat function to execute task with agent configuration
        from src.services.commonServices.common import chat
        
        # Get agent configuration
        agent_config = bridge_configurations.get(assigned_agent, {})
        
        # Build minimal request body for the agent
        request_body = {
            "user": task_description,
            "bridge_id": assigned_agent,
            "message_id": str(uuid.uuid1()),
            "thread_id": thread_id,
            "sub_thread_id": sub_thread_id,
            "org_id": org_id,
            "variables": {},
            "bridge_configurations": bridge_configurations,
            "plans": plan,  # Pass the plan context
        }
        
        # Merge agent configuration
        request_body.update(agent_config)
        
        # Call chat directly
        data_to_send = {"body": request_body, "state": {}}
        response = await chat(data_to_send)
        
        # Handle different response types
        if hasattr(response, "body"):
            # JSONResponse
            response_data = json.loads(response.body.decode("utf-8"))
            if response_data.get("success"):
                content = response_data.get("response", {}).get("data", {}).get("content", "")
                return {"success": True, "result": content}
            else:
                return {"success": False, "error": response_data.get("message", "Task execution failed")}
        elif hasattr(response, "body_iterator"):
            # StreamingResponse - consume and extract content
            accumulated_content = []
            done_event = None
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8")
                for line in chunk.split("\n"):
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                        if event.get("event") == "delta":
                            accumulated_content.append(event.get("content", ""))
                        elif event.get("event") == "done":
                            done_event = event
                    except json.JSONDecodeError:
                        pass
            
            content = "".join(accumulated_content)
            if done_event and done_event.get("response", {}).get("data", {}).get("content"):
                content = done_event["response"]["data"]["content"]
            
            return {"success": True, "result": content}
        else:
            # Dict response
            if response.get("success"):
                content = response.get("response", {}).get("data", {}).get("content", "")
                return {"success": True, "result": content}
            else:
                return {"success": False, "error": response.get("message", "Task execution failed")}

    except Exception as e:
        logger.error(f"Error executing task {task_id}: {e}")
        return {"success": False, "error": str(e)}


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

    tasks = plan.get("tasks", {})

    while True:
        # Refresh plan from store (in case of external updates like HIL responses)
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if not plan:
            break
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

        # Execute runnable tasks in parallel
        coroutines = [
            _execute_single_task(
                task_id, tasks[task_id],
                org_id, bridge_id, thread_id, sub_thread_id,
                bridge_configurations, plan,
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
