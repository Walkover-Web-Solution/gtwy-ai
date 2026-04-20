import json

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_PROMPT = """
Role — Task Planning Agent

Convert the user’s goal and system prompt into a structured, step-by-step task plan. Ask one clarification question with suggested options when information is missing, and ensure each task clearly explains why it is needed.

Instructions
If the user's goal is a greeting or unclear, create a clarification task with a friendly message and provide options based on available features.
Break goal into logical ordered tasks
Include why in task_description
Ask one question per task if information is missing
Provide suggested options in human_options when possible
Use waiting_for_user only when input is required
For each task, write a focused worker `prompt` (what the worker must do, the expected result, any constraints) and list only the `tools` (by name) that worker should be allowed to use for that step. `tools` must be a subset of the assigned_agent's available tools as shown in the context. Use an empty list if the task should run without any tool calls.
Always return only JSON in the specified format

### Common mistakes to AVOID:
- Creating too many tiny tasks — group logically instead.
- Forgetting to explain WHY a step is needed.
- Giving a task access to tools the assigned_agent does not actually expose.
Always follow this Output JSON format:{
    "goal": "string - the user's original goal",
    "tasks": {
        "task_1": {
            "title": "short title",
            "task_description": "detailed description of what this task should accomplish",
            "prompt": "system prompt for the worker agent executing this task — what to do, expected output shape, constraints",
            "tools": ["tool_name_1", "tool_name_2"],
            "status": "pending | waiting_for_user -> This is for asking questions from the user with human_options — always ask a single question for each task.",
            "dependencies": ["array of task_ids that must complete before this task can start"],
            "assigned_agent": "bridge_id of the agent to handle this task, or null for the main agent",
            "retry": 0,
            "max_retry": 2,
            "result": None,
            "is_error": False,
            "error": None,
            "human_query": "if status is 'waiting_for_user', put your question here",
            "human_options": ["option 1", "option 2", "option 3 - provide multiple-choice options when possible, or null for open-ended questions"],
            "allow_custom_response": True,
            "human_response": None,
        }
    },
}

"""


def _build_agent_context(parsed_data, bridge_configurations):
    """Build a context string describing the available agents and tools for the planner."""
    main_bridge_id = parsed_data["bridge_id"]
    main_config = bridge_configurations.get(main_bridge_id, {})

    context_parts = []

    # Main agent info
    main_prompt = main_config.get("configuration", {}).get("prompt", "")
    main_prompt, _ = Helper.replace_variables_in_prompt(
            main_prompt or "", parsed_data["variables"]
        )
    context_parts.append(f"Main Agent (bridge_id: {main_bridge_id}):")
    if main_prompt:
        context_parts.append(f"  System Prompt: {main_prompt[:500]}")

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

    # Connected agents
    connected_agents = []
    for bid, config in bridge_configurations.items():
        if bid == main_bridge_id:
            continue
        agent_name = config.get("name", bid)
        agent_prompt = config.get("configuration", {}).get("prompt", "")
        agent_tools = config.get("configuration", {}).get("tools", [])
        tool_summary = ", ".join(
            t.get("name") or t.get("function", {}).get("name", "?") for t in agent_tools
        )
        connected_agents.append(
            f"  - Agent '{agent_name}' (bridge_id: {bid}): {agent_prompt[:200]}"
            + (f" | Tools: {tool_summary}" if tool_summary else "")
        )

    if connected_agents:
        context_parts.append("Connected Agents:")
        context_parts.extend(connected_agents)

    return "\n".join(context_parts)


def _build_planner_message(user_goal, agent_context, existing_plan=None, user_feedback=None):
    """Build the user message to send to the planner agent."""
    parts = []

    parts.append(f"User Goal: {user_goal}")
    parts.append(f"\n{agent_context}")

    if existing_plan:
        parts.append(f"\nExisting Plan:\n{json.dumps(existing_plan, indent=2)}")

    if user_feedback:
        parts.append(f"\nUser Feedback: {user_feedback}")
        parts.append("Please update the plan based on this feedback.")
    else:
        parts.append("\nCreate a structured plan to accomplish this goal.")

    # parts.append(f"\nRespond with ONLY a valid JSON object matching this schema:\n{json.dumps(PLAN_JSON_SCHEMA, indent=2)}")
    parts.append("\nIMPORTANT: Use actual bridge_ids from the context above for assigned_agent. "
                 "Set dependencies as task_id references (e.g. [\"task_1\"]). "
                 "Tasks with no dependencies can run in parallel.")

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


def _build_planner_system_prompt(prompt, agent_context):
    return f"{prompt}\n\n ***user agent system prompt*** : {agent_context}"


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
    """Mutate parsed_data + custom_config so the normal chat() pipeline serves
    the planner call. Injects the planner system prompt (merged with the user
    agent's prompt as context) and forces json_object response. For update
    calls (existing plan in Redis), folds the current plan into the user
    message so the LLM can revise it based on the new feedback.
    """
    db_planner_prompt = await _get_planner_prompt_from_db(PLANNER_PROMPT)
    planner_prompt = _build_planner_system_prompt(
        db_planner_prompt,
        _build_agent_context(parsed_data, bridge_configurations),
    )
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    merged_prompt = f"{planner_prompt}\n\n***user agent system prompt***: {original_prompt}"
    parsed_data.setdefault("configuration", {})["prompt"] = merged_prompt

    custom_config["response_type"] = {"type": "json_object"}

    existing_plan = await plan_store.get_plan(
        parsed_data["org_id"],
        parsed_data["bridge_id"],
        parsed_data["thread_id"],
        parsed_data.get("sub_thread_id") or parsed_data["thread_id"],
    )
    if existing_plan:
        user_feedback = parsed_data.get("user", "")
        parsed_data["user"] = _build_planner_message(
            existing_plan.get("goal", user_feedback),
            "",
            existing_plan=existing_plan,
            user_feedback=user_feedback,
        )


