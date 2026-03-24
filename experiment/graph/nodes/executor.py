import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from graph.nodes.tools import TOOLS, TOOLS_BY_NAME
from graph.state import AgentState

EXECUTOR_SYSTEM_PROMPT = """You are a focused task executor working on a subtask as part of a larger goal.

Overall goal: {goal}

You have access to tools: {tool_names}.

RULES:
- Use tools when needed to complete the task (read files, write files, run commands, call APIs, delegate to sub-agents).
- Be specific and produce real output, not just descriptions.
- If you encounter an error, try an alternative approach before giving up.
- Your output should satisfy the acceptance criteria for this task."""

REFLECTION_PROMPT = """You are a quality reviewer. Evaluate whether the executor's output satisfies the task requirements.

Task: {task_title}
Description: {task_description}
Acceptance Criteria: {acceptance_criteria}

Executor's Output:
{result}

Respond with valid JSON:
{{
  "passed": true/false,
  "quality_score": 1-10,
  "reasoning": "Why you think it passed or failed",
  "improvement_hint": "If failed, what should be done differently"
}}"""


def _find_next_runnable_task(tasks: list[dict], completed_ids: set) -> int | None:
    """Find the next task whose dependencies are all satisfied (dependency-aware ordering)."""
    for i, task in enumerate(tasks):
        if task["status"] != "pending":
            continue
        deps = task.get("depends_on", [])
        if all(dep_id in completed_ids for dep_id in deps):
            return i
    return None


def _find_parallel_runnable_tasks(tasks: list[dict], completed_ids: set) -> list[int]:
    """Find ALL tasks whose dependencies are satisfied — these can run in parallel."""
    runnable = []
    for i, task in enumerate(tasks):
        if task["status"] != "pending":
            continue
        deps = task.get("depends_on", [])
        if all(dep_id in completed_ids for dep_id in deps):
            runnable.append(i)
    return runnable


async def _execute_single_task(
    task: dict,
    task_idx: int,
    state: AgentState,
    model: str,
    temperature: float,
    tools: list,
    tools_by_name: dict,
    system_prompt_override: str | None,
    completed: list[dict],
    scratchpad: list[dict],
) -> dict:
    """Execute a single task with ReAct loop + self-reflection."""
    tool_names = ", ".join(tools_by_name.keys())
    prompt = system_prompt_override or EXECUTOR_SYSTEM_PROMPT.format(
        goal=state["goal"], tool_names=tool_names
    )

    llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
        streaming=True,
    ).bind_tools(tools)

    messages = [SystemMessage(content=prompt)]

    # Inject scratchpad context
    if scratchpad:
        notes = "\n".join([f"- [{s.get('source_task_id', '?')}] {s['note']}" for s in scratchpad])
        messages.append(AIMessage(content=f"Context from previous work (scratchpad):\n{notes}"))

    # Inject completed task results
    if completed:
        context_summary = "\n".join(
            [f"Step '{c['title']}':\n{c['result'][:500]}" for c in completed]
        )
        messages.append(AIMessage(content=f"Previously completed steps:\n{context_summary}"))

    acceptance = task.get("acceptance_criteria", "Task completed successfully")
    messages.append(HumanMessage(
        content=(
            f"Execute this subtask:\n\n"
            f"Title: {task['title']}\n"
            f"Description: {task['description']}\n"
            f"Acceptance Criteria: {acceptance}\n"
            f"Complexity: {task.get('estimated_complexity', 'moderate')}"
        ),
    ))

    result_text = ""
    max_iterations = 15

    # ReAct loop: LLM → tool call → result → LLM → ... → final text answer
    for _ in range(max_iterations):
        response = await llm.ainvoke(messages)

        if response.tool_calls:
            messages.append(response)
            for tool_call in response.tool_calls:
                tool_fn = tools_by_name.get(tool_call["name"])
                if tool_fn:
                    try:
                        tool_result = await tool_fn.ainvoke(tool_call["args"])
                    except Exception as e:
                        tool_result = f"Tool error: {e}"
                else:
                    tool_result = f"Unknown tool: {tool_call['name']}"

                messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call["id"],
                ))
        else:
            result_text = response.content
            break

    return {
        "task_idx": task_idx,
        "task_id": task["id"],
        "title": task["title"],
        "result_text": result_text,
    }


async def _reflect_on_result(
    task: dict,
    result_text: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Self-reflection: evaluate whether the executor's output meets acceptance criteria."""
    reflection_prompt = REFLECTION_PROMPT.format(
        task_title=task["title"],
        task_description=task["description"],
        acceptance_criteria=task.get("acceptance_criteria", "Task completed successfully"),
        result=result_text[:2000],
    )

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=0.1,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    response = await llm.ainvoke([
        SystemMessage(content="You are a strict quality reviewer. Respond only with valid JSON."),
        HumanMessage(content=reflection_prompt),
    ])

    try:
        return json.loads(response.content)
    except Exception:
        return {"passed": True, "quality_score": 5, "reasoning": "Could not parse reflection", "improvement_hint": ""}


async def _run_executor(
    state: AgentState,
    model: str = "gpt-4o-mini",
    temperature: float = 0.5,
    tools: list = None,
    tools_by_name: dict = None,
    system_prompt_override: str = None,
    enable_reflection: bool = True,
    max_retries: int = 2,
) -> dict:
    """Core executor logic with dependency-aware scheduling, self-reflection, and scratchpad."""
    resolved_tools = tools or TOOLS
    resolved_tools_by_name = tools_by_name or TOOLS_BY_NAME

    tasks = list(state["tasks"])
    completed = list(state.get("completed_tasks", []))
    scratchpad = list(state.get("scratchpad", []))

    # Build set of completed task IDs
    completed_ids = {t["id"] for t in tasks if t["status"] in ("completed", "skipped")}

    # Find all parallel-runnable tasks
    runnable_indices = _find_parallel_runnable_tasks(tasks, completed_ids)

    if not runnable_indices:
        # No runnable tasks — all done or stuck
        return {
            "tasks": tasks,
            "completed_tasks": completed,
            "scratchpad": scratchpad,
        }

    # Execute runnable tasks in parallel
    execute_coros = []
    for idx in runnable_indices:
        task = tasks[idx]
        tasks[idx] = {**task, "status": "in_progress"}
        execute_coros.append(
            _execute_single_task(
                task=task,
                task_idx=idx,
                state=state,
                model=model,
                temperature=temperature,
                tools=resolved_tools,
                tools_by_name=resolved_tools_by_name,
                system_prompt_override=system_prompt_override,
                completed=completed,
                scratchpad=scratchpad,
            )
        )

    results = await asyncio.gather(*execute_coros, return_exceptions=True)

    needs_replan = False
    replan_reason = None

    for res in results:
        if isinstance(res, Exception):
            # Find the task that failed and mark it
            for idx in runnable_indices:
                if tasks[idx]["status"] == "in_progress":
                    tasks[idx] = {**tasks[idx], "status": "failed", "result": str(res)}
                    needs_replan = True
                    replan_reason = f"Task '{tasks[idx]['title']}' raised exception: {res}"
                    break
            continue

        task_idx = res["task_idx"]
        task = tasks[task_idx]
        result_text = res["result_text"]

        # Self-reflection
        reflection = None
        if enable_reflection and task.get("estimated_complexity", "moderate") != "simple":
            reflection_result = await _reflect_on_result(
                task, result_text, state["api_key"], model
            )

            quality_score = reflection_result.get("quality_score", 5)
            passed = reflection_result.get("passed", True)
            reflection = json.dumps(reflection_result)

            # Retry if reflection says quality is poor
            if not passed and quality_score < 5:
                retry_count = 0
                while retry_count < max_retries and not passed:
                    retry_count += 1
                    hint = reflection_result.get("improvement_hint", "Try a different approach")
                    # Re-execute with the hint
                    retry_res = await _execute_single_task(
                        task={**task, "description": f"{task['description']}\n\nPREVIOUS ATTEMPT FEEDBACK: {hint}"},
                        task_idx=task_idx,
                        state=state,
                        model=model,
                        temperature=temperature,
                        tools=resolved_tools,
                        tools_by_name=resolved_tools_by_name,
                        system_prompt_override=system_prompt_override,
                        completed=completed,
                        scratchpad=scratchpad,
                    )
                    result_text = retry_res["result_text"]

                    reflection_result = await _reflect_on_result(
                        task, result_text, state["api_key"], model
                    )
                    passed = reflection_result.get("passed", True)
                    quality_score = reflection_result.get("quality_score", 5)
                    reflection = json.dumps(reflection_result)

                if not passed:
                    # Still failed after retries — mark as failed, trigger re-plan
                    tasks[task_idx] = {
                        **task,
                        "status": "failed",
                        "result": result_text,
                        "reflection": reflection,
                    }
                    needs_replan = True
                    replan_reason = f"Task '{task['title']}' failed quality check after {max_retries} retries: {reflection_result.get('reasoning', '')}"
                    continue

        # Task succeeded
        tasks[task_idx] = {
            **task,
            "status": "completed",
            "result": result_text,
            "reflection": reflection,
        }
        completed.append({"title": task["title"], "result": result_text})

        # Update scratchpad with key findings
        scratchpad.append({
            "source_task_id": task["id"],
            "note": f"[{task['title']}] Output: {result_text[:300]}",
        })

    # Find next task index for backward compatibility (first pending task)
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
        "needs_replan": needs_replan,
        "replan_reason": replan_reason,
    }


async def executor_node(state: AgentState) -> dict:
    """Default executor node — uses built-in tools and gpt-4o-mini with self-reflection."""
    return await _run_executor(state)


def make_executor_node(agent_config: dict, tools: list):
    """Factory: creates an executor node with dynamic tools and agent config."""
    model = agent_config.get("model", "gpt-4o-mini")
    temperature = agent_config.get("temperature", 0.5)
    agent_system_prompt = agent_config.get("system_prompt", "")

    tools_by_name = {t.name: t for t in tools}
    tool_names = ", ".join(tools_by_name.keys())

    custom_prompt = None
    if agent_system_prompt:
        custom_prompt = (
            f"{agent_system_prompt}\n\n"
            f"You are executing a subtask as part of a larger goal: {{goal}}\n"
            f"Available tools: {tool_names}\n\n"
            f"RULES:\n"
            f"- Use tools when needed to complete the task.\n"
            f"- Be specific and produce real output, not just descriptions."
        )

    async def dynamic_executor_node(state: AgentState) -> dict:
        prompt = custom_prompt.replace("{goal}", state["goal"]) if custom_prompt else None
        return await _run_executor(
            state,
            model=model,
            temperature=temperature,
            tools=tools,
            tools_by_name=tools_by_name,
            system_prompt_override=prompt,
        )

    return dynamic_executor_node
