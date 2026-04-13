import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field as PydanticField, create_model

from globals import logger
from workflow.llm import create_llm, extract_text_from_response, extract_json_from_text
from workflow.tool_adapter import build_tool_payload_hint, normalize_tool_payload
from workflow.prompts import EXECUTOR_SYSTEM_PROMPT, REFLECTION_PROMPT, Markers


def _find_parallel_runnable_tasks(tasks: list[dict], completed_ids: set[str]) -> list[int]:
    runnable = []
    for index, task in enumerate(tasks):
        if task["status"] != "pending":
            continue
        if all(dep_id in completed_ids for dep_id in task.get("depends_on", [])):
            runnable.append(index)
    return runnable


def _is_tool_error_response(tool_result: str) -> bool:
    if not isinstance(tool_result, str):
        return False
    lowered = tool_result.strip().lower()
    if not lowered:
        return True
    return (
        lowered.startswith("tool error:")
        or lowered.startswith("unknown tool:")
        or "validation error" in lowered
    )


def _create_ask_planner_tool() -> StructuredTool:
    AskPlannerArgs = create_model(
        "WorkflowAskPlannerArgs",
        question=(str, PydanticField(description="Clarification question for the planner.")),
    )

    async def _ask_planner(question: str) -> str:
        return f"{Markers.ASK_PLANNER}:{question}"

    return StructuredTool.from_function(
        coroutine=_ask_planner,
        name="ask_planner",
        description="Ask the planner for clarification when the task is blocked or ambiguous.",
        args_schema=AskPlannerArgs,
    )


async def _reflect_on_result(task: dict, result_text: str, api_key: str, model: str, service: str = "openai") -> dict:
    """Quality review of executor output. Returns {passed, quality_score, reasoning, improvement_hint}."""
    llm = create_llm(model=model, api_key=api_key, temperature=0.1, json_mode=True, service=service)
    prompt = REFLECTION_PROMPT.format(
        task_title=task["title"],
        task_description=task["description"],
        acceptance_criteria=task.get("acceptance_criteria", ""),
        result=result_text,
    )
    try:
        response = await llm.ainvoke([SystemMessage(content=prompt),
                                      HumanMessage(content="Evaluate now.")])
        return extract_json_from_text(extract_text_from_response(response))
    except Exception as e:
        logger.error(f"[Workflow] Reflection failed for task '{task['title']}': {e}")
        return {"passed": True, "quality_score": 7, "reasoning": "Reflection failed", "improvement_hint": ""}


async def _execute_single_task(
    task: dict,
    task_idx: int,
    state: dict,
    model: str,
    temperature: float,
    all_tools: list,
    tools_by_name: dict,
    completed: list,
    scratchpad: list,
    saved_messages: list | None = None,
) -> dict:
    """Execute one task with ReAct loop. Returns a result dict."""
    tool_names = ", ".join(tools_by_name.keys())
    system_prompt = EXECUTOR_SYSTEM_PROMPT.format(goal=state["goal"], tool_names=tool_names)

    llm = create_llm(
        model=model, api_key=state["api_key"], temperature=temperature,
        streaming=True, tools=all_tools,
        service=state.get("user_config", {}).get("executor_service", "openai"),
    )

    planner_response = state.get("planner_response")
    if saved_messages and planner_response and state.get("worker_question_task_id") == task["id"]:
        # Resume after planner clarification — restore conversation + inject answer
        messages = list(saved_messages)
        if messages and isinstance(messages[-1], ToolMessage):
            messages[-1] = ToolMessage(
                content=f"Planner's answer: {planner_response}",
                tool_call_id=messages[-1].tool_call_id,
            )
        else:
            messages.append(HumanMessage(content=f"Planner's answer: {planner_response}"))
    else:
        # Build dependency context from completed tasks
        dep_context = ""
        dep_ids = task.get("depends_on", [])
        if dep_ids and completed:
            dep_lines = []
            for c in completed:
                if c.get("id") in dep_ids or c.get("title") in [t.get("title") for t in state.get("tasks", []) if t.get("id") in dep_ids]:
                    dep_lines.append(f"- {c['title']}: {c['result'][:500]}")
            if dep_lines:
                dep_context = f"\n\nResults from dependent tasks:\n" + "\n".join(dep_lines)

        task_desc = (
            f"Execute this task.\n\n"
            f"Title: {task['title']}\n"
            f"Description: {task['description']}\n"
            f"Acceptance Criteria: {task.get('acceptance_criteria', '')}\n"
            f"Assigned Tool: {task.get('tool_name') or 'Best matching tool'}"
            f"{dep_context}"
        )
        messages = [SystemMessage(content=system_prompt)]
        if scratchpad:
            notes = "\n".join(f"- {s.get('note', '')}" for s in scratchpad)
            messages.append(AIMessage(content=f"Context from previous work:\n{notes}"))
        messages.append(HumanMessage(content=task_desc))

    result_text = ""
    saw_any_tool_call = False
    unresolved_tool_error = False
    last_tool_error = ""
    asked_planner: str | None = None
    max_retries = (state.get("user_config") or {}).get("max_retries", 10) or 10

    for _ in range(max_retries):
        response = await llm.ainvoke(messages)
        if not response.tool_calls:
            result_text = response.content or ""
            break
        messages.append(response)
        saw_any_tool_call = True
        for tool_call in response.tool_calls:
            if tool_call["name"] == "ask_planner":
                asked_planner = (tool_call.get("args") or {}).get("question", "")
                # Save messages so we can resume after planner answers
                placeholder = ToolMessage(content="Waiting for planner...", tool_call_id=tool_call["id"])
                messages.append(placeholder)
                return {
                    "task_idx": task_idx,
                    "result_text": "",
                    "asked_planner": asked_planner,
                    "messages": messages,
                    "saw_any_tool_call": saw_any_tool_call,
                    "unresolved_tool_error": False,
                }
            tool_fn = tools_by_name.get(tool_call["name"])
            if tool_fn is None:
                tool_result_str = f"Unknown tool: {tool_call['name']}"
            else:
                try:
                    call_args = normalize_tool_payload(tool_fn, tool_call.get("args") or {})
                    raw = await tool_fn.ainvoke(call_args)
                    tool_result_str = raw if isinstance(raw, str) else json.dumps(raw)
                except Exception as e:
                    tool_result_str = f"Tool error: {e}. {build_tool_payload_hint(tool_fn)}"
            unresolved_tool_error = _is_tool_error_response(tool_result_str)
            if unresolved_tool_error:
                last_tool_error = tool_result_str
            messages.append(ToolMessage(content=tool_result_str, tool_call_id=tool_call["id"]))

    return {
        "task_idx": task_idx,
        "result_text": result_text,
        "asked_planner": None,
        "messages": None,
        "saw_any_tool_call": saw_any_tool_call,
        "unresolved_tool_error": unresolved_tool_error,
        "last_tool_error": last_tool_error,
    }


def make_executor_node(tools: list[StructuredTool]):
    ask_planner_tool = _create_ask_planner_tool()
    all_tools = [*tools, ask_planner_tool]
    tools_by_name = {tool.name: tool for tool in all_tools}

    async def executor_node(state: dict) -> dict:
        config = state.get("user_config") or {}
        model = config.get("executor_model", "gpt-4o-mini")
        temperature = config.get("executor_temperature", 0.5)
        enable_reflection = config.get("enable_reflection", True)
        max_retries = config.get("max_retries", 2) or 2
        logger.info(f"[Workflow] Executor node started (model={model})")

        tasks = list(state.get("tasks") or [])
        completed = list(state.get("completed_tasks") or [])
        scratchpad = list(state.get("scratchpad") or [])
        saved_messages = state.get("worker_messages")

        completed_ids = {t["id"] for t in tasks if t["status"] in ("completed", "skipped")}
        runnable = _find_parallel_runnable_tasks(tasks, completed_ids)
        if not runnable:
            return {"tasks": tasks, "completed_tasks": completed, "scratchpad": scratchpad}

        needs_worker_clarification = False
        worker_question: str | None = None
        worker_question_task_id: str | None = None
        worker_saved_messages: list | None = None
        needs_replan = False
        replan_reason: str | None = None

        # Run all dependency-free tasks in parallel
        # Track which coroutine corresponds to which task index
        coros_with_idx = [
            (_execute_single_task(
                task=tasks[idx],
                task_idx=idx,
                state=state,
                model=model,
                temperature=temperature,
                all_tools=all_tools,
                tools_by_name=tools_by_name,
                completed=completed,
                scratchpad=scratchpad,
                saved_messages=saved_messages,
            ), idx)
            for idx in runnable
        ]
        results = await asyncio.gather(*[coro for coro, _ in coros_with_idx], return_exceptions=True)

        # Match results to task indices correctly
        for (_, task_idx), res in zip(coros_with_idx, results):
            if isinstance(res, Exception):
                # Mark the CORRECT task that raised as failed
                tasks[task_idx] = {**tasks[task_idx], "status": "failed", "result": str(res)}
                needs_replan = True
                replan_reason = f"Task '{tasks[task_idx]['title']}' raised exception: {res}"
                continue

            task_idx = res["task_idx"]
            task = tasks[task_idx]
            result_text = res["result_text"]

            if res.get("asked_planner"):
                tasks[task_idx] = {**task, "status": "pending"}
                needs_worker_clarification = True
                worker_question = res["asked_planner"]
                worker_question_task_id = task["id"]
                worker_saved_messages = res.get("messages")
                continue

            if res.get("saw_any_tool_call") and res.get("unresolved_tool_error"):
                err = res.get("last_tool_error") or "Tool response validation failed."
                tasks[task_idx] = {**task, "status": "failed",
                                   "result": f"{result_text}\n\nUnresolved tool error: {err}"}
                needs_replan = True
                replan_reason = f"Task '{task['title']}' ended with unresolved tool error: {err}"
                continue

            # Self-reflection (skip for simple tasks)
            reflection = None
            if enable_reflection and task.get("estimated_complexity", "moderate") != "simple":
                reflection_result = await _reflect_on_result(task, result_text, state["api_key"], model, service=config.get("executor_service", "openai"))
                passed = reflection_result.get("passed", True)
                reflection = json.dumps(reflection_result)

                retry_count = 0
                while not passed and reflection_result.get("quality_score", 5) < 5 and retry_count < max_retries:
                    retry_count += 1
                    hint = reflection_result.get("improvement_hint", "Try a different approach")
                    retry_res = await _execute_single_task(
                        task={**task, "description": f"{task['description']}\n\nPREVIOUS ATTEMPT FEEDBACK: {hint}"},
                        task_idx=task_idx,
                        state=state,
                        model=model,
                        temperature=temperature,
                        all_tools=all_tools,
                        tools_by_name=tools_by_name,
                        completed=completed,
                        scratchpad=scratchpad,
                    )
                    result_text = retry_res["result_text"]
                    reflection_result = await _reflect_on_result(task, result_text, state["api_key"], model, service=config.get("executor_service", "openai"))
                    passed = reflection_result.get("passed", True)
                    reflection = json.dumps(reflection_result)

                if not passed and reflection_result.get("quality_score", 5) < 5:
                    tasks[task_idx] = {**task, "status": "failed", "result": result_text, "reflection": reflection}
                    needs_replan = True
                    replan_reason = f"Task '{task['title']}' failed quality check after {max_retries} retries: {reflection_result.get('reasoning', '')}"
                    continue

            tasks[task_idx] = {**task, "status": "completed", "result": result_text, "reflection": reflection}
            completed.append({"title": task["title"], "result": result_text})
            scratchpad.append({"source_task_id": task["id"], "note": f"[{task['title']}] {result_text[:300]}"})

        next_idx = len(tasks)
        for i, t in enumerate(tasks):
            if t["status"] == "pending":
                next_idx = i
                break

        return {
            "tasks": tasks,
            "completed_tasks": completed,
            "current_task_index": next_idx,
            "scratchpad": scratchpad,
            "step_approved": False,  # Reset for next cycle — ensures fresh approval for subsequent tasks
            "needs_replan": needs_replan,
            "replan_reason": replan_reason,
            "needs_worker_clarification": needs_worker_clarification,
            "worker_question": worker_question,
            "worker_question_task_id": worker_question_task_id,
            "worker_messages": worker_saved_messages if needs_worker_clarification else None,
            "planner_response": None,
        }

    return executor_node
