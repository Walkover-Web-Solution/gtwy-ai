import json
import re

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_PROMPT = """
"""

def _has_task_ids_in_message(user_message):
    """Check if user message contains task IDs in format like 'task_id:task_1' or 'task_id: task_2'"""
    if not user_message:
        return False
    task_pattern = re.compile(r'task_id\s*:\s*task_\d+', re.IGNORECASE)
    return bool(task_pattern.search(user_message))


def _has_question_ids_in_message(user_message):
    """Check if user message contains question IDs in format like 'question_id:q1'"""
    if not user_message:
        return False
    question_pattern = re.compile(r'question_id\s*:\s*q\d+', re.IGNORECASE)
    return bool(question_pattern.search(user_message))


def _extract_task_answer_pairs(user_message):
    """Extract task-answer pairs from human-loop message.

    Format: 'task_id:task_1, answer:Use preset...'
    Returns dict: {"task_1": "Use preset...", "task_2": "answer2", ...}
    """
    if not user_message:
        return {}

    pattern = re.compile(
        r'task_id\s*:\s*(task_\d+)\s*,\s*answer\s*:\s*([^\n]+?)(?=\s*task_id\s*:|$)',
        re.IGNORECASE | re.DOTALL
    )
    matches = pattern.findall(user_message)
    return {task_id.strip(): answer.strip() for task_id, answer in matches}


def _extract_question_answer_pairs(user_message):
    """Extract question-answer pairs from human-loop message.

    Format: 'question_id:q1, answer:Gmail\\nquestion_id:q2, answer:Every email'
    Returns dict: {"q1": "Gmail", "q2": "Every email", ...}
    """
    if not user_message:
        return {}

    pattern = re.compile(
        r'question_id\s*:\s*(q\d+)\s*,\s*answer\s*:\s*([^\n]+?)(?=\s*question_id\s*:|$)',
        re.IGNORECASE | re.DOTALL
    )
    matches = pattern.findall(user_message)
    return {q_id.strip(): answer.strip() for q_id, answer in matches}



def _separate_search_and_other_tools(tools):
    """Separate tools into search tools and other tools.

    A tool with only a 'search' param goes to search_tools only.
    A tool with both 'search' and other params goes to both lists.
    A tool with no 'search' param goes to other_tools only.
    """
    search_tools = []
    other_tools = []

    for tool in tools:
        properties = tool.get("properties") or {}
        is_search = "search" in properties
        has_other_params = "executor" in properties
        if is_search:
            search_tools.append(tool)
        if not is_search or has_other_params:
            other_tools.append(tool)
    
    return search_tools, other_tools


def _build_agent_context(parsed_data, bridge_configurations, other_tools=None):

    main_bridge_id = parsed_data["bridge_id"]

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
        context_parts.append("\nTools available for task execution (do NOT set assigned_agent for these — they run on the main agent):")
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
    is_question_loop=False,
):
    """Build the user message for the planner agent."""
    parts = []

    if existing_plan:
        if existing_plan.get("questions"):
            parts.append("\nQuestions:")
            parts.append(json.dumps(existing_plan["questions"], indent=2, default=str))

        if user_feedback:
            parts.append(f"\nUser Message: {user_feedback}")

        if is_question_loop:
            parts.append("\nMark answered questions as 'answered' and continue planning. Do not regenerate the full plan.")
    else:
        parts.append(user_goal)

    return "\n".join(parts)

def _build_planner_system_prompt(prompt, agent_context, existing_plan=None, session_memory=None, user_system_prompt=None):
    system_prompt_parts = []

    # if user_system_prompt:
    #     system_prompt_parts.append(f"User Agent System Prompt:\n{user_system_prompt}")

    system_prompt_parts.append(f"\nAvailable Agents and Tools:\n{agent_context}")

    if existing_plan:
        if existing_plan.get("plan"):
            system_prompt_parts.append(
                "\nPreviously built plan by you (AI):\n"
                + json.dumps(existing_plan["plan"], indent=2, default=str)
            )

    system_prompt_content = "\n".join(system_prompt_parts)
    final_prompt = (user_system_prompt or "") + "\n" + system_prompt_content

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

    # Check if user message is a human-loop response
    user_input = parsed_data.get("user", "")
    has_task_ids = _has_task_ids_in_message(user_input)
    has_question_ids = _has_question_ids_in_message(user_input)

    # Separate search tools from other tools
    original_tools = parsed_data.get("configuration", {}).get("tools", [])
    search_tools, other_tools = _separate_search_and_other_tools(original_tools)
    
    # Set planner to use ONLY search tools in its configuration
    parsed_data.setdefault("configuration", {})["tools"] = search_tools
    # Also update custom_config directly — it was already built from parsed_data["configuration"]
    # before prepare_planner_request is called, so parsed_data changes alone won't affect the LLM call.
    if search_tools:
        custom_config["tools"] = search_tools
    else:
        custom_config.pop("tools", None)

    # Build conversation from history_summary so the planner gets prior context
    # as a proper conversation message instead of raw JSON blobs from thread history.
    conversation = []
    if existing_plan and existing_plan.get("history_summary"):
        history_summary = existing_plan["history_summary"]
        if not isinstance(history_summary, str):
            history_summary = json.dumps(history_summary)
        conversation = [{"role": "assistant", "content": history_summary}]
    parsed_data.setdefault("configuration", {})["conversation"] = conversation
    
    # Build system prompt with agent context (includes other_tools) + session memory + user system prompt
    db_planner_prompt = await _get_planner_prompt_from_db(PLANNER_PROMPT)
    agent_context = _build_agent_context(parsed_data, bridge_configurations, other_tools)
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    planner_prompt = _build_planner_system_prompt(db_planner_prompt, agent_context, existing_plan, session_memory, original_prompt)
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
            is_question_loop=has_question_ids,
        )
    else:
        # First-time plan creation: just the user goal
        parsed_data["user"] = _build_planner_message(
            user_goal=user_input,
            is_human_loop=False,
        )


