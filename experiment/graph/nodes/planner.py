import json
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from graph.state import AgentState

PLANNER_SYSTEM_PROMPT = """You are an elite AI task planner — similar to how Cursor AI and Windsurf plan complex tasks.
Your job is to deeply analyze the user's goal, then either ask a targeted clarifying question OR produce a high-quality execution plan.

You MUST respond with valid JSON in one of two modes:

MODE 1 — QUESTION (only when the goal is genuinely ambiguous and you cannot proceed without user input):
{
  "mode": "question",
  "reasoning": "Brief explanation of why you need clarification",
  "question": {
    "text": "Your specific clarifying question",
    "options": ["Option A", "Option B", "Option C"]
  },
  "tasks": []
}

MODE 2 — TASKS (when you have enough context to create an actionable plan):
{
  "mode": "tasks",
  "reasoning": "Your step-by-step analysis of the goal, what needs to happen, and why you chose this decomposition",
  "question": null,
  "tasks": [
    {
      "title": "Concise step title",
      "description": "Detailed description of what to do, including specific inputs/outputs expected",
      "depends_on": [],
      "priority": "high",
      "acceptance_criteria": "What 'done' looks like — specific, verifiable condition",
      "estimated_complexity": "simple"
    }
  ]
}

PLANNING RULES:
1. THINK DEEPLY before decomposing. Analyze the goal, identify all sub-problems, and consider the optimal execution order.
2. Each task must be ATOMIC — small enough for a single focused execution, but large enough to be meaningful.
3. Set depends_on to reference task indices (0-based) when a task needs output from a previous task. Tasks with no dependencies can run in PARALLEL.
4. Estimate complexity honestly: "simple" (1 tool call or direct answer), "moderate" (2-5 tool calls, some reasoning), "complex" (multi-step reasoning, multiple tools, error handling).
5. Priority: "high" = critical path, "medium" = important but not blocking, "low" = nice to have.
6. Acceptance criteria must be SPECIFIC and VERIFIABLE — not vague like "task is done".
7. Keep tasks between 2-8 for most goals. Only go higher for truly complex multi-phase work.
8. If the user already answered a clarifying question, incorporate their answer and proceed directly to MODE 2.
9. Only use MODE 1 when you genuinely cannot produce a reasonable plan. Prefer making reasonable assumptions and noting them in your reasoning.
"""

REPLAN_SYSTEM_PROMPT = """You are an elite AI task planner performing a RE-PLAN.

A previous plan was being executed but a task FAILED. You must analyze the failure and produce an ADJUSTED plan for the remaining work.

CONTEXT:
- Original goal: {goal}
- Tasks completed so far: {completed_summary}
- Failed task: {failed_task}
- Failure reason: {failure_reason}
- Scratchpad (accumulated context): {scratchpad}

You MUST respond with valid JSON:
{
  "mode": "tasks",
  "reasoning": "Analysis of what went wrong and how you're adjusting the plan",
  "question": null,
  "tasks": [
    {
      "title": "Step title",
      "description": "What to do — account for the failure and any context from completed tasks",
      "depends_on": [],
      "priority": "high",
      "acceptance_criteria": "Verifiable done condition",
      "estimated_complexity": "simple"
    }
  ]
}

RULES:
1. Do NOT repeat already-completed tasks. Build on their results.
2. Address the failure — either retry with a different approach or work around it.
3. Keep the plan focused on what remains to achieve the original goal.
4. Reference scratchpad context when available — it contains findings from previous tasks.
"""


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
            "status": "pending",
            "result": None,
            "depends_on": resolved_deps,
            "priority": task.get("priority", "medium"),
            "acceptance_criteria": task.get("acceptance_criteria", "Task completed successfully"),
            "estimated_complexity": task.get("estimated_complexity", "moderate"),
            "reflection": None,
        })

    return tasks


async def _run_planner(state: AgentState, model: str = "gpt-4o", temperature: float = 0.3, system_prompt_override: str = None) -> dict:
    """Core planner logic with deep planning, dependency detection, and re-plan support."""

    # Detect if this is a re-plan triggered by a failed task
    is_replan = state.get("needs_replan", False)

    if is_replan:
        prompt = _build_replan_prompt(state)
    else:
        prompt = system_prompt_override or PLANNER_SYSTEM_PROMPT

    user_message = _build_user_message(state)

    llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_message),
    ])

    parsed = json.loads(response.content)
    mode = parsed.get("mode", "tasks")

    if mode == "question" and parsed.get("question"):
        return {
            "needs_question": True,
            "question_text": parsed["question"]["text"],
            "question_options": parsed["question"].get("options", []),
            "tasks": [],
            "needs_replan": False,
            "replan_reason": None,
        }

    tasks = _parse_tasks(parsed)
    revision_count = state.get("plan_revision_count", 0)
    if is_replan:
        revision_count += 1

    return {
        "needs_question": False,
        "question_text": None,
        "question_options": None,
        "tasks": tasks,
        "human_input": None,
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
    """Default planner node — uses gpt-4o for planning, with deep analysis."""
    return await _run_planner(state)


def make_planner_node(agent_config: dict):
    """Factory: creates a planner node parameterized by agent DB config."""
    model = agent_config.get("planner_model", agent_config.get("model", "gpt-4o"))
    temperature = agent_config.get("temperature", 0.3)
    agent_system_prompt = agent_config.get("system_prompt", "")

    custom_prompt = PLANNER_SYSTEM_PROMPT
    if agent_system_prompt:
        custom_prompt = (
            f"You are acting as the planner for an AI agent with the following persona:\n"
            f"---\n{agent_system_prompt}\n---\n\n"
            f"{PLANNER_SYSTEM_PROMPT}"
        )

    async def dynamic_planner_node(state: AgentState) -> dict:
        return await _run_planner(state, model=model, temperature=temperature, system_prompt_override=custom_prompt)

    return dynamic_planner_node
