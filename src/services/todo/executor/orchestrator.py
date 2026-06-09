import asyncio
import json
import time

from globals import logger
from src.services.todo import plan_store
from src.services.todo.executor.constants import INTERRUPTED_TASK_RECOVERY_NOTE
from src.services.todo.executor.metrics import finalize_metrics, init_metrics
from src.services.todo.executor.plan_tasks import (
    get_runnable_tasks,
    get_tasks,
    is_plan_blocked,
    is_plan_complete,
    recover_interrupted_tasks,
    set_tasks,
)
from src.services.todo.executor.task_runner import execute_single_task


async def execute_plan(
    org_id: str,
    bridge_id: str,
    thread_id: str,
    sub_thread_id: str,
    bridge_configurations: dict,
    parsed_data: dict,
    streamer=None,
) -> dict | None:
    """Execute all tasks in dependency order, running independent tasks in parallel.

    Returns the finalised main-agent metrics aggregate so the caller can attach
    it to the history update. Connected-agent tasks persist their own rows via
    the normal chat path and are excluded from the returned metrics.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        logger.error(f"Plan not found for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
        return None

    recovered_task_ids = recover_interrupted_tasks(plan)
    if recovered_task_ids:
        logger.warning(f"Recovered interrupted tasks: {recovered_task_ids}")
        await plan_store.update_plan(plan)

    main_agent_metrics = init_metrics()
    plan["state"] = "executing"
    await plan_store.update_plan(plan)

    async def _emit(event_type: str, data: dict) -> None:
        if streamer:
            await streamer.emit_delta(json.dumps({"event": event_type, **data}))

    if recovered_task_ids:
        await _emit("tasks_recovered", {
            "task_ids": recovered_task_ids,
            "reason": INTERRUPTED_TASK_RECOVERY_NOTE,
        })

    plan_variables = parsed_data.get("variables") or {}
    plan_variables_path = parsed_data.get("variables_path") or {}
    max_iterations = 1000

    for _ in range(max_iterations):
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if not plan:
            logger.warning(f"Plan disappeared during execution for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            break
        tasks = get_tasks(plan)

        if is_plan_complete(tasks):
            has_failures = any(t["status"] == "failed" for t in tasks.values())
            plan["state"] = "failed" if has_failures else "completed"
            await plan_store.update_plan(plan)
            logger.info(f"Plan terminal state={plan['state']} for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            # UI already holds the plan from creation + incremental task events;
            # only the terminal state is needed here, not the full plan body.
            await _emit("plan_completed", {"state": plan["state"]})
            break

        if is_plan_blocked(tasks):
            plan["state"] = "paused"
            await plan_store.update_plan(plan)
            logger.info(f"Plan paused for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            waiting_task_ids = [
                tid for tid, t in tasks.items() if t.get("status") == "waiting_for_user"
            ]
            await _emit("plan_paused", {"state": "paused", "waiting_task_ids": waiting_task_ids})
            break

        runnable = get_runnable_tasks(tasks)
        if not runnable:
            await asyncio.sleep(1)
            continue

        logger.info(f"[EXECUTE] Runnable tasks: {runnable}")
        for task_id in runnable:
            tasks[task_id]["status"] = "in_progress"
            tasks[task_id]["started_at"] = time.time()
            await _emit("task_started", {"task_id": task_id, "title": tasks[task_id].get("title", "")})
        set_tasks(plan, tasks)
        await plan_store.update_plan(plan)

        coroutines = [
            execute_single_task(
                task_id, tasks[task_id],
                org_id, bridge_id, thread_id, sub_thread_id,
                bridge_configurations, plan,
                streamer=streamer,
                main_agent_metrics=main_agent_metrics,
                variables=plan_variables,
                variables_path=plan_variables_path,
            )
            for task_id in runnable
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        tasks = get_tasks(plan)

        for task_id, result in zip(runnable, results):
            task = tasks[task_id]

            if isinstance(result, Exception):
                result = {"success": False, "status": "failed", "error": str(result)}

            status = result.get("status") or ("completed" if result.get("success") else "failed")

            history = result.get("history") if isinstance(result.get("history"), dict) else None
            if history is not None:
                task["history"] = history

            if status == "completed":
                task["status"] = "completed"
                task["is_error"] = False
                task["error"] = None
                task["result"] = result.get("result")
                await _emit("task_completed", {
                    "task_id": task_id,
                    "title": task.get("title", ""),
                    "status": "completed",
                    "result": task["result"],
                    "questions": result.get("questions"),
                })

            elif status == "waiting_for_user":
                questions = result.get("questions")
                task["status"] = "waiting_for_user"
                task["is_error"] = False
                task["questions"] = questions
                task["result"] = result.get("result")
                task["human_response"] = None
                await _emit("task_waiting_for_user", {
                    "task_id": task_id,
                    "title": task.get("title", ""),
                    "status": "waiting_for_user",
                    "result": task["result"],
                    "questions": questions,
                })

            else:  # failed
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
                        "status": "failed",
                        "result": result.get("result"),
                        "questions": result.get("questions"),
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
                        "status": "failed",
                        "result": result.get("result"),
                        "questions": result.get("questions"),
                        "is_error": True,
                        "error": task["error"] or "task execution failed",
                        "retry": task["retry"],
                        "max_retry": max_retry,
                        "retrying": False,
                    })

        set_tasks(plan, tasks)
        await plan_store.update_plan(plan)

    else:
        logger.error(
            f"Plan execution hit max iterations ({max_iterations}) — "
            f"possible infinite loop for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}"
        )
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if plan:
            plan["state"] = "failed"
            await plan_store.update_plan(plan)
            await _emit("plan_failed", {"state": "failed", "reason": "max_iterations_reached"})

    # Safety guard: resolve any plan still marked 'executing' after the loop.
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if plan and plan["state"] == "executing":
        has_failures = any(t["status"] == "failed" for t in get_tasks(plan).values())
        plan["state"] = "failed" if has_failures else "completed"
        await plan_store.update_plan(plan)
        await _emit("plan_completed", {"state": plan["state"]})

    return finalize_metrics(main_agent_metrics)
