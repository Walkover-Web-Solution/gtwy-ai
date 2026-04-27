import json
import re

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_PROMPT = """
RoleYou are the Planner Agent — the strategic thinking layer of an Agentic AI Platform.
Your Job
* Convert the user’s system prompt and request into a clear, executable plan.
* Use available tools (including search) to gather information, but do not include search tasks in the plan.
* Create tasks only after proper research.
* In update mode, carefully analyze the existing plan. Update only impacted tasks (especially those waiting for user input), not the entire logic unnecessarily.
* The executor will follow your execution_details, so ensure instructions are precise and complete.
* Your output replaces the entire plan—update carefully without losing important tasks.

Instructions 
-  Research is YOUR job, not the executor's. If search/information tools are available, use them during planning to fetch internal data the LLM may not know. Resolve what you can before emitting the plan.
- You should update the plan based on the user’s input. Your output must fully replace the existing plan, so ensure no important tasks are lost.
- Ask questions when required, based on the user agent’s system prompt not guess the need of task.

Output Rules
Return only a valid JSON object. No markdown, no commentary, no extra text.

{
  "goal": "user's original goal in one sentence",
  "tasks": {
    "task_1": {
      "title": "short human-readable title",
      "task_description": "what this task should accomplish, in plain language",
      "status": "pending | waiting_for_user",
      "dependencies": ["task_id", "..."],
      "assigned_agent": "bridge_id of the agent, or null to use the main agent",
      "assigned_tool": "name of tool (give correct and same name as in the tool list), or null",
      "retry": 0,
      "max_retry": 2,
      "result": null,
      "is_error": false,
      "error": null,
      "human_query": "single clear question, only when status = waiting_for_user; else null", #No need to add the options here 
      "human_options": ["option 1", "option 2"], #give a options of the qeustions when available
      "allow_custom_response": true,
      "human_response": null,
      "execution_details": "precise instructions the executor needs executer has only permission for task related tool so give the tool name and parameters properly"
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


def _build_planner_message(
    user_goal,
    agent_context=None,
    existing_plan=None,
    user_feedback=None,
    user_system_prompt=None,
):
    """Build the user message to send to the planner agent."""
    parts = []

    if user_system_prompt:
        parts.append(f"## USER AGENT SYSTEM PROMPT\n{user_system_prompt}")
        parts.append("\n Use search tools to gather required information related to executions tool call and parameters. Do not add search tasks to the plan. Do not guess any task details.")

    if existing_plan:
        # REPLAN FLOW — strong guardrails to keep planner focused
        plan_json = json.dumps(existing_plan, indent=2, default=str)
        parts.append(f"\n## CURRENT PLAN\n{plan_json}")
        parts.append(f"\n## User's client message\n{user_feedback or 'None'}")
     
    else:
        parts.append(f"##  Your message is: \n{user_goal}")

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
                q = (qa.get("question") or "")[:200]
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

    # Build system prompt with agent context + session memory + existing plan context
    db_planner_prompt = await _get_planner_prompt_from_db(PLANNER_PROMPT)
    agent_context = _build_agent_context(parsed_data, bridge_configurations)
    planner_prompt = _build_planner_system_prompt(db_planner_prompt, agent_context, session_memory)
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    parsed_data.setdefault("configuration", {})["prompt"] = planner_prompt

    custom_config["response_type"] = {"type": "json_object"}

    # Build concise user message; heavy context lives in system prompt
    user_input = parsed_data.get("user", "")
    if existing_plan:
        # Update flow
        parsed_data["user"] = _build_planner_message(
            user_goal=existing_plan.get("goal"),
            user_feedback=user_input,
            user_system_prompt=original_prompt,
        )
    else:
        # First-time plan creation: just the user goal
        parsed_data["user"] = _build_planner_message(
            user_goal=user_input,
            user_system_prompt=original_prompt,
        )


