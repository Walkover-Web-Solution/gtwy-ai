import json
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from graph.state import AgentState

PLANNER_SYSTEM_PROMPT = """\
You are a senior AI planner. Analyze the user's goal thoroughly, then respond with **valid JSON** in one of two modes.

# Mode 1 — ASK (only when genuinely ambiguous)
Use this ONLY when critical information is missing and you truly cannot plan without it.
Prefer making reasonable assumptions over asking.
```json
{"mode":"question","reasoning":"...","question":{"text":"...","options":["A","B","C"]},"tasks":[]}
```

# Mode 2 — PLAN (default)
Decompose the goal into concrete, executable tasks.
```json
{"mode":"tasks","reasoning":"...","question":null,"tasks":[{...}]}
```

## Task schema
| Field                  | Description                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| title                  | Short action-oriented title                                                 |
| tool_name              | Name of the tool to call for this task (MUST match an available tool name)  |
| description            | Precise instructions: what to do, inputs, expected output                   |
| depends_on             | List of 0-based task indices this task depends on. Empty = can run parallel  |
| priority               | "high" (critical path) / "medium" / "low"                                   |
| acceptance_criteria    | Specific, verifiable condition that defines "done"                          |
| estimated_complexity   | "simple" (single action) / "moderate" (2-5 steps) / "complex" (multi-step)  |

## Planning principles
- Think step-by-step: identify sub-problems, dependencies, and optimal execution order.
- Each task must be atomic — one clear unit of work a single worker can execute.
- Maximize parallelism: only add depends_on when output from a prior task is truly required.
- Target 2-8 tasks. Go beyond only for genuinely complex multi-phase goals.
- If the user already answered a question, use their answer and go straight to Mode 2.
- **Every task description MUST map to one or more of the available tools.** Do not plan tasks that no tool can execute.
- Reference tool names explicitly in the task description so the executor knows which tool to call.
"""

RESEARCH_SYSTEM_PROMPT = """\
You are a senior AI planner in the RESEARCH phase. Before creating the execution plan, you may call tools to gather information needed for better planning.

You have access to tools. Use them to look up services, plugins, APIs, or any data you need to understand before planning.

When you have gathered enough information, respond with EXACTLY: __RESEARCH_DONE__
Followed by a brief summary of what you learned.

Do NOT produce the plan yet — just gather information and signal when done.
"""

REPLAN_SYSTEM_PROMPT = """\
You are re-planning after a task failure. Analyze what went wrong and produce an adjusted plan.

## Failure context
- **Goal:** {goal}
- **Completed tasks (PRESERVED — do NOT repeat these):** {completed_summary}
- **Failed task:** {failed_task}
- **Failure reason:** {failure_reason}
- **Scratchpad (context from previous work):** {scratchpad}

## Rules
- **NEVER repeat completed tasks.** They are already done and their results are preserved.
- Only produce NEW tasks to replace the failed task and any remaining work.
- Fix the failure with a different approach or work around it.
- Use scratchpad context and completed task results when planning the new tasks.
- If you need critical information from the user before re-planning, use Mode 1 (question).

# Mode 1 — ASK (only when genuinely needed)
```json
{{"mode":"question","reasoning":"...","question":{{"text":"...","options":["A","B","C"]}},"tasks":[]}}
```

# Mode 2 — PLAN (default)
Provide ONLY the new/replacement tasks. Do NOT include already-completed tasks.
```json
{{"mode":"tasks","reasoning":"...","question":null,"tasks":[{{"title":"...","tool_name":"...","description":"...","depends_on":[],"priority":"high","acceptance_criteria":"...","estimated_complexity":"simple"}}]}}
```
"""


def _format_tool_schemas(tool_schemas: list) -> str:
    """Format tool schemas into a readable block for prompt injection."""
    if not tool_schemas:
        return ""

    lines = ["## Available tools", "The executor has access to these tools. Plan tasks that use them.", ""]
    for t in tool_schemas:
        params = t.get("parameters", [])
        if params:
            param_parts = []
            for p in params:
                req = "required" if p.get("required") else "optional"
                param_parts.append(f"    - `{p['name']}` ({p.get('type', 'string')}, {req}): {p.get('description', '')}")
            param_block = "\n".join(param_parts)
            lines.append(f"**{t['name']}** — {t['description']}\n  Parameters:\n{param_block}\n")
        else:
            lines.append(f"**{t['name']}** — {t['description']}\n")

    return "\n".join(lines)


def _build_user_message(state: AgentState) -> str:
    """Build the user message with all available context."""
    goal = state["goal"]
    human_input = state.get("human_input")
    scratchpad = state.get("scratchpad", [])

    parts = [f"GOAL: {goal}"]

    if human_input:
        parts.append(f"\nUser's answer to previous question: {human_input}")

    if scratchpad:
        notes = "\n".join([f"- [{s.get('source_task_id', '?')}] {s['note']}" for s in scratchpad])
        parts.append(f"\nAccumulated context from previous work:\n{notes}")

    return "\n".join(parts)


def _parse_tasks(parsed: dict) -> list[dict]:
    """Parse task list from LLM response into TaskItem-compatible dicts."""
    tasks = []
    raw_tasks = parsed.get("tasks", [])
    task_ids = []

    # First pass: assign IDs
    for _ in raw_tasks:
        task_ids.append(str(uuid.uuid4())[:8])

    # Second pass: build tasks with resolved depends_on
    for i, task in enumerate(raw_tasks):
        raw_deps = task.get("depends_on", [])
        resolved_deps = []
        for dep in raw_deps:
            if isinstance(dep, int) and 0 <= dep < len(task_ids):
                resolved_deps.append(task_ids[dep])
            elif isinstance(dep, str) and dep in task_ids:
                resolved_deps.append(dep)

        tasks.append({
            "id": task_ids[i],
            "title": task.get("title", f"Task {i+1}"),
            "description": task.get("description", ""),
            "tool_name": task.get("tool_name"),
            "status": "pending",
            "result": None,
            "depends_on": resolved_deps,
            "priority": task.get("priority", "medium"),
            "acceptance_criteria": task.get("acceptance_criteria", "Task completed successfully"),
            "estimated_complexity": task.get("estimated_complexity", "moderate"),
            "reflection": None,
        })

    return tasks


async def _run_research_phase(state: AgentState, tools: list, model: str, temperature: float, prompt: str, user_message: str, max_rounds: int = 5) -> tuple[str, list]:
    """Run a tool-calling research loop so the planner can gather info before planning.
    
    Returns (research_context_string, thinking_steps) where thinking_steps is a list
    of structured dicts for UI visibility.
    """
    tools_by_name = {t.name: t for t in tools}

    research_llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
    ).bind_tools(tools)

    messages = [
        SystemMessage(content=RESEARCH_SYSTEM_PROMPT + "\n\n" + prompt),
        HumanMessage(content=user_message),
    ]

    research_notes = []
    thinking_steps = []

    for round_num in range(max_rounds):
        response = await research_llm.ainvoke(messages)
        messages.append(response)

        # Capture any reasoning text the LLM emits
        if response.content:
            thinking_steps.append({
                "type": "reasoning",
                "content": response.content,
                "round": round_num + 1,
            })

        # If no tool calls, research is done
        if not response.tool_calls:
            if response.content:
                research_notes.append(response.content)
            break

        # Execute each tool call
        for tc in response.tool_calls:
            tool_fn = tools_by_name.get(tc["name"])
            if tool_fn:
                try:
                    result = await tool_fn.ainvoke(tc["args"])
                    result_str = str(result) if not isinstance(result, str) else result
                except Exception as e:
                    result_str = f"Tool error: {e}"
            else:
                result_str = f"Tool '{tc['name']}' not found."

            messages.append(ToolMessage(content=result_str, tool_call_id=tc["id"]))
            research_notes.append(f"[{tc['name']}] {result_str[:500]}")

            thinking_steps.append({
                "type": "tool_call",
                "tool_name": tc["name"],
                "args": tc.get("args", {}),
                "result": result_str[:500],
                "round": round_num + 1,
            })

    return "\n".join(research_notes), thinking_steps


CLARIFICATION_SYSTEM_PROMPT = """\
You are the planner answering a question from a worker who is executing a subtask.

Overall goal: {goal}

The worker is stuck on task "{task_title}" and asks:
"{worker_question}"

You have access to tools to research the answer. Use them if needed.

Rules:
- If you can answer the question, respond with JSON: {{"can_answer": true, "answer": "your detailed answer"}}
- If you CANNOT answer and need the user's input, respond with JSON: {{"can_answer": false, "question_for_user": "the question to ask the user", "options": ["option1", "option2"]}}
- Be specific and helpful — the worker depends on your guidance.
"""


async def _handle_worker_question(state: AgentState, model: str = None, temperature: float = None, tools: list = None) -> dict:
    """Handle a clarification question from a worker.
    
    The planner tries to answer using its knowledge and tools.
    If it can't, it escalates to the user.
    """
    config = state.get("user_config") or {}
    resolved_model = model or config.get("planner_model", "gpt-4o")
    resolved_temp = temperature if temperature is not None else config.get("planner_temperature", 0.3)

    worker_question = state.get("worker_question", "")
    task_id = state.get("worker_question_task_id", "")

    # Find the task that asked
    task_title = "Unknown task"
    for t in state.get("tasks", []):
        if t["id"] == task_id:
            task_title = t["title"]
            break

    prompt = CLARIFICATION_SYSTEM_PROMPT.format(
        goal=state["goal"],
        task_title=task_title,
        worker_question=worker_question,
    )

    # If tools available, run a quick research loop to gather info for the answer
    research_context = ""
    if tools:
        research_context, _ = await _run_research_phase(
            state, tools, resolved_model, resolved_temp, prompt, worker_question, max_rounds=3
        )
        if research_context:
            prompt += f"\n\nResearch findings:\n{research_context}"

    llm = ChatOpenAI(
        model=resolved_model,
        api_key=state["api_key"],
        temperature=resolved_temp,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=f"Worker question: {worker_question}"),
    ])

    try:
        parsed = json.loads(response.content)
    except Exception:
        parsed = {"can_answer": True, "answer": response.content}

    if parsed.get("can_answer", True):
        # Planner can answer → send response back to executor
        return {
            "planner_response": parsed.get("answer", response.content),
            "needs_worker_clarification": False,
            "worker_question": None,
            # Keep worker_question_task_id so executor knows which task to resume
            "needs_question": False,
            "needs_replan": False,
        }
    else:
        # Planner can't answer → escalate to user
        return {
            "needs_question": True,
            "question_text": parsed.get("question_for_user", worker_question),
            "question_options": parsed.get("options", []),
            "needs_worker_clarification": False,
            "worker_question": worker_question,  # preserve for context
            "needs_replan": False,
        }


async def _run_planner(state: AgentState, model: str = None, temperature: float = None, system_prompt_override: str = None, tools: list = None) -> dict:
    """Core planner logic with deep planning, dependency detection, and re-plan support.
    
    If tools are provided, runs a research phase first (tool-calling loop) to gather
    information, then generates the plan with that context.
    
    Reads configuration from state['user_config'] with fallback to function params and defaults.
    """
    config = state.get("user_config") or {}

    resolved_model = model or config.get("planner_model", "gpt-4o")
    resolved_temp = temperature if temperature is not None else config.get("planner_temperature", 0.3)

    # Handle worker clarification question (different path from normal planning)
    if state.get("needs_worker_clarification"):
        return await _handle_worker_question(state, resolved_model, resolved_temp, tools)

    # Detect if this is a re-plan triggered by a failed task
    is_replan = state.get("needs_replan", False)

    # Build tool schemas block for prompt injection
    tool_schemas = state.get("tool_schemas") or []
    tool_block = _format_tool_schemas(tool_schemas)

    if is_replan:
        base_prompt = _build_replan_prompt(state)
        if tool_block:
            base_prompt = f"{base_prompt}\n\n{tool_block}"
    else:
        # Build prompt: system_prompt_override > user_config.system_prompt > default
        agent_persona = config.get("system_prompt", "")
        if system_prompt_override:
            base_prompt = system_prompt_override
        elif agent_persona:
            base_prompt = (
                f"You are acting as the planner for an AI agent with the following persona:\n"
                f"---\n{agent_persona}\n---\n\n"
                f"{PLANNER_SYSTEM_PROMPT}"
            )
        else:
            base_prompt = PLANNER_SYSTEM_PROMPT

        # Append tool schemas so planner knows what's executable
        if tool_block:
            base_prompt = f"{base_prompt}\n\n{tool_block}"

    user_message = _build_user_message(state)

    # Phase 1: Research (optional — planner has access to all tools and decides which to call)
    research_context = ""
    thinking_steps = []
    if tools:
        research_context, thinking_steps = await _run_research_phase(
            state, tools, resolved_model, resolved_temp, base_prompt, user_message
        )

    # Phase 2: Plan generation (JSON response)
    plan_prompt = base_prompt
    if research_context:
        plan_prompt = (
            f"{base_prompt}\n\n"
            f"## Research findings (gathered via tool calls)\n"
            f"{research_context}"
        )

    llm = ChatOpenAI(
        model=resolved_model,
        api_key=state["api_key"],
        temperature=resolved_temp,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    response = await llm.ainvoke([
        SystemMessage(content=plan_prompt),
        HumanMessage(content=user_message),
    ])

    parsed = json.loads(response.content)
    mode = parsed.get("mode", "tasks")

    # Capture planner reasoning
    reasoning = parsed.get("reasoning", "")
    if reasoning:
        thinking_steps.append({"type": "plan_reasoning", "content": reasoning})

    if mode == "question" and parsed.get("question"):
        result = {
            "needs_question": True,
            "question_text": parsed["question"]["text"],
            "question_options": parsed["question"].get("options", []),
            "planner_thinking": thinking_steps,
        }
        if is_replan:
            # Preserve existing tasks (completed ones stay) and keep replan flag
            # so after user answers, planner re-enters replan mode
            result["needs_replan"] = True
            result["replan_reason"] = state.get("replan_reason")
        else:
            result["tasks"] = []
            result["needs_replan"] = False
            result["replan_reason"] = None
        return result

    new_tasks = _parse_tasks(parsed)
    revision_count = state.get("plan_revision_count", 0)
    if is_replan:
        revision_count += 1

    if is_replan:
        # Preserve completed/skipped tasks and remove failed/pending ones, then append new plan
        existing_tasks = state.get("tasks", [])
        preserved = [t for t in existing_tasks if t["status"] in ("completed", "skipped")]
        merged_tasks = preserved + new_tasks

        # current_task_index should point to the first new task
        next_idx = len(preserved)

        return {
            "needs_question": False,
            "question_text": None,
            "question_options": None,
            "tasks": merged_tasks,
            "human_input": None,
            "planner_thinking": thinking_steps,
            "needs_replan": False,
            "replan_reason": None,
            "plan_revision_count": revision_count,
            "current_task_index": next_idx,
        }

    return {
        "needs_question": False,
        "question_text": None,
        "question_options": None,
        "tasks": new_tasks,
        "human_input": None,
        "planner_thinking": thinking_steps,
        "needs_replan": False,
        "replan_reason": None,
        "plan_revision_count": revision_count,
        "current_task_index": 0,
    }


def _build_replan_prompt(state: AgentState) -> str:
    """Build the re-plan system prompt with failure context."""
    completed = state.get("completed_tasks", [])
    completed_summary = "\n".join(
        [f"- {c['title']}: {c['result'][:200]}" for c in completed]
    ) if completed else "None"

    # Find the failed task
    failed_task = "Unknown"
    failure_reason = state.get("replan_reason", "Unknown failure")
    for t in state.get("tasks", []):
        if t.get("status") == "failed":
            failed_task = f"{t['title']}: {t['description']}"
            if t.get("result"):
                failure_reason = t["result"]
            break

    scratchpad = state.get("scratchpad", [])
    scratchpad_text = "\n".join(
        [f"- [{s.get('source_task_id', '?')}] {s['note']}" for s in scratchpad]
    ) if scratchpad else "Empty"

    return REPLAN_SYSTEM_PROMPT.format(
        goal=state["goal"],
        completed_summary=completed_summary,
        failed_task=failed_task,
        failure_reason=failure_reason,
        scratchpad=scratchpad_text,
    )


async def planner_node(state: AgentState) -> dict:
    """Default planner node — reads config from state['user_config']."""
    return await _run_planner(state)


def make_planner_node(agent_config: dict, tools: list = None):
    """Factory: creates a planner node parameterized by agent DB config.
    
    If tools are provided, the planner can call them during a research phase
    to gather information before generating the plan.
    """
    # Pre-compute agent-level defaults from DB config
    agent_defaults = {
        "planner_model": agent_config.get("planner_model", agent_config.get("model", "gpt-4o")),
        "planner_temperature": agent_config.get("temperature", 0.3),
        "system_prompt": agent_config.get("system_prompt", ""),
    }
    planner_tools = tools or []

    async def dynamic_planner_node(state: AgentState) -> dict:
        # Merge: agent DB defaults < state user_config (user overrides win)
        merged_config = {**agent_defaults, **(state.get("user_config") or {})}
        merged_state = {**state, "user_config": merged_config}
        return await _run_planner(merged_state, tools=planner_tools)

    return dynamic_planner_node
