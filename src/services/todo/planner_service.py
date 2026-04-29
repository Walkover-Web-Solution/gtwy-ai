import json
import re

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_PROMPT = """
Top level Role: Planner
Top Level Objective: Turn the user's goal into a structured, executable task list. If the goal is unclear, ask targeted questions before planning. All tasks must be planned within its capabilities — so the executor runs them sequentially with zero additional decision-making.


Planner Context and instructions - 
{{system_prompt}}


return Goal and proper organised plan in below Output Format
{
  "user_message":"used it when you are talking to user directly, like greeting or casual conversation",
  "plan": {
    "goal": "Build a REST API for a todo app",
    "tasks": [
      {
        "id": "task_1",
        "title": "Design database schema",
        "status": "pending"|| "waiting_for_user",
        "dependencies": [],
        "assigned_agent": null,
        "assigned_tool": null,
        "execution_details": "Create PostgreSQL schema with users and todos tables",
        "response":""
      }
    ]
  },

  "questions": [
    {
      "id": "q1",
      "for_task": "task_1",
      "status":"pending" | "answered" | "skipped",
      "question": "Which database do you want to use?",
      "options": ["PostgreSQL", "MySQL", "SQLite"],
      "allow_custom": true,
      "priority": "blocking" | "optional",
      "response": null
    }
  ]
}
"""

def _has_task_ids_in_message(user_message):
    """Check if user message contains task IDs in format like 'task_id:task_1' or 'task_id: task_2'"""
    if not user_message:
        return False
    
    # Pattern to match 'task_id:task_X' format (human-loop response)
    task_pattern = re.compile(r'task_id\s*:\s*task_\d+', re.IGNORECASE)
    return bool(task_pattern.search(user_message))


def _extract_task_answer_pairs(user_message):
    """Extract task-answer pairs from human-loop message.
    
    Format: 'task_id:task_1, answer:Use preset...'
    Returns dict: {"task_1": "Use preset...", "task_2": "answer2", ...}
    """
    if not user_message:
        return {}
    
    # Pattern to match: task_id:task_X, answer:ANSWER_TEXT
    # Handles multiple task-answer pairs
    pattern = re.compile(
        r'task_id\s*:\s*(task_\d+)\s*,\s*answer\s*:\s*([^\n]+?)(?=\s*task_id\s*:|$)',
        re.IGNORECASE | re.DOTALL
    )
    
    matches = pattern.findall(user_message)
    return {task_id.strip(): answer.strip() for task_id, answer in matches}


def _is_search_tool(tool):
    """Check if a tool is a search tool by examining its parameters.
    
    A tool is considered a search tool if its parameters schema contains a 'search' field.
    """
    properties = tool.get("properties") or {}
    return "search" in properties


def _separate_search_and_other_tools(tools):
    """Separate tools into search tools and other tools.
    
    Args:
        tools: List of tool configurations
    
    Returns:
        Tuple of (search_tools, other_tools)
    """
    search_tools = []
    other_tools = []
    
    for tool in tools:
        if _is_search_tool(tool):
            search_tools.append(tool)
        else:
            other_tools.append(tool)
    
    return search_tools, other_tools


def _build_agent_context(parsed_data, bridge_configurations, other_tools=None):
    """Build a context string describing the available agents and tools for the planner.
    
    Args:
        parsed_data: Request data
        bridge_configurations: Configuration for all agents
        other_tools: Non-search tools to list in system prompt (optional)
    """
    main_bridge_id = parsed_data["bridge_id"]
    main_config = bridge_configurations.get(main_bridge_id, {})
    # Get variables_path from bridge configuration (contains AI-fillable parameter mappings)
    variables_path = main_config.get("variables_path", {})

    context_parts = []

    # Connected agents info
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

    # Add non-search tools to system prompt for task assignment
    if other_tools:
        context_parts.append("\nAdditional Available Tools (for task assignment only):")
        context_parts.append("You cannot directly call these tools, but you can assign them to tasks for the executor to run.")
        for tool in other_tools:
            name = tool.get("name") or tool.get("function", {}).get("name", "unknown")
            desc = tool.get("description") or tool.get("function", {}).get("description", "")
            param_info = tool.get("properties", {})
            
            context_parts.append(f"  Tool Name: {name}")
            context_parts.append(f"  Tool Description: {desc}")
            context_parts.append(f"  Tool Parameters: {param_info}")

    return "\n".join(context_parts)


def _build_planner_message(
    user_goal,
    existing_plan=None,
    user_feedback=None,
    is_human_loop=False,
):
    """Build the user message for the planner agent.

    Args:
        is_human_loop: True when the user is responding to task-related questions
    """
    parts = []

    if existing_plan:
        parts.append("Current Plan:")
        parts.append(json.dumps(existing_plan, indent=2, default=str))

        if is_human_loop:
            parts.append("\nUser Responses (for task-related questions):")
            parts.append(f"- {user_feedback}")
            parts.append(
                "\nUpdate only the related tasks. Do not update the full plan and not change your goal."
            )
        else:
            parts.append(
                "\nUpdate the plan based on the user's request. Preserve completed tasks and their results."
            )

    else:
        parts.append(f"User Goal: {user_goal}")

    return "\n".join(parts)

def _build_planner_system_prompt(prompt, agent_context, session_memory=None, user_system_prompt=None):
    # Build the system_prompt section to inject into the template
    system_prompt_parts = []
    
    if user_system_prompt:
        system_prompt_parts.append(f"User Agent System Prompt:\n{user_system_prompt}")
    
    system_prompt_parts.append(f"\nAvailable Agents and Tools:\n{agent_context}")
    # Inject system_prompt into the template
    system_prompt_content = "\n".join(system_prompt_parts)
    final_prompt = prompt.replace("{{system_prompt}}", system_prompt_content)
    
    return final_prompt


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
            return default_prompt
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

    # Check if user message contains task IDs and plan exists
    user_input = parsed_data.get("user", "")
    has_task_ids = _has_task_ids_in_message(user_input)
    
    # Separate search tools from other tools
    original_tools = parsed_data.get("configuration", {}).get("tools", [])
    search_tools, other_tools = _separate_search_and_other_tools(original_tools)
    
    # Set planner to use ONLY search tools in its configuration
    parsed_data.setdefault("configuration", {})["tools"] = search_tools
    
    # Build system prompt with agent context (includes other_tools) + session memory + user system prompt
    db_planner_prompt = await _get_planner_prompt_from_db(PLANNER_PROMPT)
    agent_context = _build_agent_context(parsed_data, bridge_configurations, other_tools)
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    planner_prompt = _build_planner_system_prompt(db_planner_prompt, agent_context, session_memory, original_prompt)
    parsed_data.setdefault("configuration", {})["prompt"] = planner_prompt

    custom_config["response_type"] = {"type": "json_object"}

    # Build concise user message; heavy context lives in system prompt
    if existing_plan:
        # Update flow - pass is_human_loop flag to optimize message format
        parsed_data["user"] = _build_planner_message(
            user_goal=existing_plan.get("goal"),
            existing_plan=existing_plan,
            user_feedback=user_input,
            is_human_loop=has_task_ids,
        )
    else:
        # First-time plan creation: just the user goal
        parsed_data["user"] = _build_planner_message(
            user_goal=user_input,
            is_human_loop=False,
        )


