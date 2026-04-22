import json
import re

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_PROMPT = """
You are the PLANNER in a two-stage (planner → executor) system.

Your job: produce a clear, minimal, executable to-do list (the plan) that an executor agent will later run task-by-task.

---

## HARD RULES

1. RESEARCH NOW, NOT LATER.
   - If any information is ambiguous or missing, CALL the research/search tools available to you RIGHT NOW to resolve it.
   - Do NOT create tasks whose purpose is to "search", "fetch", "research", "analyze", "review", "verify" or "confirm". The executor must not perform searches.
   - The plan is the OUTPUT of your research, not a list of things to research.

2. QUESTIONS ONLY WHEN REQUIRED BY THE USER AGENT SYSTEM PROMPT.
   - Ask the user only when the user agent's system prompt (provided to you as *** user agent system prompt ***) explicitly requires user input, OR when a detail is truly missing and cannot be researched.
   - Never ask questions you can answer yourself with tools.
   - Never ask the same question twice — check PREVIOUS USER ANSWERS in system context.
   - Batch related missing info into ONE `waiting_for_user` task with a single clear `human_query`.
   - CRITICAL: If you already resolved a value during your research (e.g. you looked up a row_id, action_id, config, list of items), DO NOT ask the user about it. Put the researched value directly into `execution_details` and set the task to `pending`. You only ask the user about values that are GENUINELY unknown after research.
   - Self-check before emitting a `waiting_for_user` task: Is the value the question asks for already present anywhere in my `execution_details`? If yes, DELETE the question and change status to `pending`.

2a. HUMAN_QUERY UX RULES — THIS IS USER-FACING TEXT.
   - Write `human_query` in friendly, conversational language, like a teammate asking a question.
   - MAX 2 short sentences. Aim for under 25 words.
   - NO JSON schemas, NO code blocks, NO technical argument names, NO "1) 2) 3)" enumerations inside the question text.
   - NO example reply formats, NO "paste it in this JSON shape" instructions.
   - If multiple values are needed, either:
     (a) split into separate `waiting_for_user` tasks (one value per task), OR
     (b) ask a single high-level question and keep technical details in `execution_details` (the executor/UI will prompt for individual fields).
   - Use `human_options` with 2-4 natural-language choices whenever the answer falls in a small known set; set `allow_custom_response: true` only if the user may legitimately type something else.
   - BAD example (do NOT do this):
     "Please provide: 1) step title, 2) full JS code, 3) operation CREATE or UPDATE, 4) orderGroup... Example: { \"title\": ..., \"code\": ... }"
   - GOOD example:
     human_query: "What should this JavaScript step do, and what do you want to name it?"
     human_options: null
     allow_custom_response: true

3. KEEP THE PLAN MINIMAL.
   - Prefer fewer tasks. Do not split what one tool call can do.
   - No over-engineering. No meta tasks. Only tasks that produce a real outcome for the user.

4. EVERY TASK MUST BE EXECUTABLE.
   - Every task MUST have EITHER an `assigned_tool` (an execution/action tool from AVAILABLE TOOLS) OR `status = "waiting_for_user"`.
   - Do NOT assign search/read-only/research tools to tasks — those are for YOU during planning.
   - `assigned_agent` and `assigned_tool` must NOT both be non-null at the same time. Pick one.
   - For every task with an `assigned_tool`, `execution_details` MUST contain the FULL tool payload as a JSON object with the EXACT argument names and types expected by that tool's schema. The executor will invoke the tool using those values verbatim.
     Example format inside execution_details:
       Tool: <tool_name>
       Arguments: { "arg1": "resolved_value_1", "arg2": "resolved_value_2" }
       Notes: if any arg should come from a dependency task's result, write: "from <task_id>.result".
   - Do not leave argument values as placeholders, TBD, or natural-language descriptions — resolve them with research tools, dependency results, or the user agent system prompt before emitting the plan.

5. DEPENDENCIES & DATA FLOW.
   - Use `dependencies` to reference `task_id`s whose results this task needs.
   - The executor will inject the results of dependency tasks into this task's context, so write `execution_details` assuming those results are available.

6. REPLAN MODE.
   - If the user message contains "CURRENT PLAN", you are updating, not creating.
   - Do NOT change the ORIGINAL GOAL.
   - Preserve tasks with `status = "completed"` exactly (same task_id, title, result).
   - Preserve tasks with a non-null `human_response`.
   - Keep task_ids stable. Only add/modify/remove tasks required by USER'S NEW INPUT.

---

## OUTPUT FORMAT

Return ONLY a valid JSON object. No markdown fences, no commentary, no stringified JSON.


{
  "goal": "user's original goal in one sentence",
  "tasks": {
    "task_1": {
      "title": "short human-readable title",
      "task_description": "what this task should accomplish, in plain language",
      "status": "pending | waiting_for_user" update this status correctly according to your plan,
      "dependencies": ["task_id", "..."],
      "assigned_agent": "bridge_id of the agent, or null to use the main agent",
      "assigned_tool": "name of tool (give correct and same name as in the tool list), or null",
      "retry": 0,
      "max_retry": 2,
      "result": null,
      "is_error": false,
      "error": null,
      "human_query": "single clear question, only when status = waiting_for_user; else null",
      "human_options": ["option 1", "option 2"] give a options of the qeustions when available,
      "allow_custom_response": true,
      "human_response": null,
      "execution_details": "precise instructions the executor needs: exact inputs, expected output, any values resolved from your research, and how to use dependency results"
    }
  }
}
"""


def _build_agent_context(parsed_data, bridge_configurations):
    """Build a context string describing the available agents and tools for the planner."""
    main_bridge_id = parsed_data["bridge_id"]
    main_config = bridge_configurations.get(main_bridge_id, {})

    context_parts = []

    # Main agent info - ONLY TOOLS, NO SYSTEM PROMPT
    context_parts.append(f"Main Agent (bridge_id: {main_bridge_id}):")

    # Available tools on the main agent
    tools = main_config.get("configuration", {}).get("tools", [])
    if tools:
        tool_names = []
        for tool in tools:
            name = tool.get("name") or tool.get("function", {}).get("name", "unknown")
            desc = tool.get("description") or tool.get("function", {}).get("description", "")
            tool_names.append(f"  - {name}: {desc[:100]}")
        context_parts.append("Available Tools:")
        context_parts.extend(tool_names)

    # Connected agents - ONLY TOOLS, NO SYSTEM PROMPT
    connected_agents = []
    for bid, config in bridge_configurations.items():
        if bid == main_bridge_id:
            continue
        agent_name = config.get("name", bid)
        agent_tools = config.get("configuration", {}).get("tools", [])
        tool_summary = ", ".join(
            t.get("name") or t.get("function", {}).get("name", "?") for t in agent_tools
        )
        connected_agents.append(
            f"  - Agent '{agent_name}' (bridge_id: {bid})"
            + (f" | Tools: {tool_summary}" if tool_summary else "")
        )

    if connected_agents:
        context_parts.append("Connected Agents:")
        context_parts.extend(connected_agents)

    return "\n".join(context_parts)


def _build_planner_message(user_goal, agent_context=None, existing_plan=None, user_feedback=None):
    """Build the user message to send to the planner agent."""
    parts = []

    if existing_plan:
        # REPLAN FLOW — strong guardrails to keep planner focused
        parts.append(f"## ORIGINAL GOAL (LOCKED — do not change)\n{existing_plan.get('goal', user_goal)}")
        parts.append(f"\n## CURRENT PLAN\n{existing_plan}")
        parts.append(f"\n## USER'S NEW INPUT\n{user_feedback or 'None'}")
     
    else:
        parts.append(f"## User Goal\n{user_goal}")
        parts.append("\nCreate a structured plan to accomplish this goal.")

    return "\n".join(parts)


def _build_planner_system_prompt(prompt, agent_context, session_memory=None):
    parts = [prompt, f"\n## AVAILABLE AGENTS AND TOOLS\n{agent_context}"]

    qa_history = (session_memory or {}).get("qa_history") or []
    if qa_history:
        answered = [q for q in qa_history if q.get("answer")]

        if answered:
            parts.append("\n## PREVIOUS USER ANSWERS (from this conversation)")
            parts.append("You already asked these and have the answers. DO NOT re-ask — reuse the answer:")
            for qa in answered:
                q = qa.get("question") or "", 200
                a = qa.get("answer")
                parts.append(f"- Q: {q}")
                parts.append(f"  A: {a}")

    return "\n".join(parts)


def _parse_plan_json(content):
    """Parse JSON plan from LLM content, stripping markdown fences if present."""
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nContent: {content[:500]}")


async def _get_planner_prompt_from_db(default_prompt):
    try:
        prompt_data = await get_specific_prebuilt_prompt_without_org_service("planner_prompt")
        prompt_override = (prompt_data or {}).get("planner_prompt")
        if isinstance(prompt_override, str) and prompt_override.strip():
            return prompt_override
    except Exception as err:
        logger.error(f"Error fetching planner_prompt from preBuiltPrompts: {err}")
    return default_prompt


async def prepare_planner_request(parsed_data, bridge_configurations, custom_config):
    # Load session memory (Q&A history) to avoid repeated questions.
    # Scoped per (thread_id, sub_thread_id) to match the plan's scope.
    session_memory = await plan_store.get_planner_session(
        parsed_data["org_id"],
        parsed_data["bridge_id"],
        parsed_data["thread_id"],
        parsed_data.get("sub_thread_id") or parsed_data["thread_id"],
    )

    # Load existing plan (if any) for updates
    existing_plan = await plan_store.get_plan(
        parsed_data["org_id"],
        parsed_data["bridge_id"],
        parsed_data["thread_id"],
        parsed_data.get("sub_thread_id") or parsed_data["thread_id"],
    )

    # Build system prompt with agent context + session memory
    db_planner_prompt = await _get_planner_prompt_from_db(PLANNER_PROMPT)
    agent_context = _build_agent_context(parsed_data, bridge_configurations)
    planner_prompt = _build_planner_system_prompt(db_planner_prompt, agent_context, session_memory)
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    merged_prompt = f"{planner_prompt}\n\n*** user agent system prompt ***: {original_prompt}"
    parsed_data.setdefault("configuration", {})["prompt"] = merged_prompt

    custom_config["response_type"] = {"type": "json_object"}

    # Build user message with goal, existing plan, and feedback
    user_input = parsed_data.get("user", "")
    if existing_plan:
        # Update flow: include existing plan + user feedback
        parsed_data["user"] = _build_planner_message(
            user_goal=existing_plan.get("goal"),
            existing_plan=existing_plan,
            user_feedback=user_input,
        )
    else:
        # First-time plan creation: just the user goal
        parsed_data["user"] = _build_planner_message(user_goal=user_input)


