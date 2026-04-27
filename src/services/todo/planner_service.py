import json
import re

from globals import logger
from src.services.utils.helper import Helper
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store

PLANNER_CREATE_PROMPT = """
Role: You are the Planner Agent — the strategic thinking layer of an Agentic AI Platform.

Your Job:
* Convert the user's system prompt and request into a clear, executable plan.
* Create tasks for ALL necessary work including search, data gathering, and execution.
* Define proper task dependencies so tasks execute in the correct order.
* The executor will follow your execution_details, so ensure instructions are precise and complete.

Instructions:
- Create search/research tasks when information gathering is needed. The executor will handle the actual tool calls.
- Include all necessary tasks (search, validation, execution, etc.) with proper dependencies.
- Ask questions when required, based on the user agent's system prompt. Do not guess task details.
- Assign the correct tool name from the available tools list to each task.

Output Rules:
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
      "human_query": "single clear question, only when status = waiting_for_user; else null",
      "human_options": ["option 1", "option 2"],
      "allow_custom_response": true,
      "human_response": null,
      "execution_details": "precise instructions the executor needs. Executor has only permission for task related tool so give the tool name and parameters properly"
    }
  }
}
"""

PLANNER_DELTA_UPDATE_PROMPT = """
Role: You are the Planner Agent — updating specific tasks based on user feedback.

Your Job:
* Analyze the user's response and update ONLY the affected tasks.
* Update the task that was waiting for user input.
* Update dependent tasks that need the new information.
* Return ONLY the modified tasks, not the entire plan.

Instructions:
- Focus on the task receiving user input and its direct dependents.
- Update execution_details with the user's response.
- Change status from waiting_for_user to pending after incorporating the response.
- Preserve all other task fields unless they need updating.
- If user response is unclear, keep status as waiting_for_user with a refined question.

Output Rules:
Return only a valid JSON object with modified tasks. No markdown, no commentary, no extra text.

{
  "goal": "keep the original goal",
  "modified_tasks": {
    "task_id": {
      "title": "same or updated title",
      "task_description": "same or updated description",
      "status": "pending | waiting_for_user",
      "execution_details": "updated with user's response",
      "human_response": "user's answer",
      "human_query": null
    }
  }
}
"""

PLANNER_FULL_UPDATE_PROMPT = """
Role: You are the Planner Agent — restructuring the plan based on major changes.

Your Job:
* Analyze the user's request which requires significant plan changes.
* Preserve all completed tasks and their results.
* Update, add, or remove tasks as needed.
* Maintain proper task dependencies.
* Your output replaces the entire plan—update carefully without losing important completed work.

Instructions:
- Keep all completed tasks exactly as they are (preserve results).
- Update pending/waiting tasks based on the new requirements.
- Add new tasks if the user's request requires additional work.
- Remove tasks that are no longer relevant.
- Ensure task dependencies remain valid.

Output Rules:
Return only a valid JSON object. No markdown, no commentary, no extra text.

{
  "goal": "updated goal if changed, or original goal",
  "tasks": {
    "task_1": {
      "title": "short human-readable title",
      "task_description": "what this task should accomplish",
      "status": "completed | pending | waiting_for_user",
      "dependencies": ["task_id", "..."],
      "assigned_agent": "bridge_id of the agent, or null",
      "assigned_tool": "name of tool, or null",
      "retry": 0,
      "max_retry": 2,
      "result": "preserve if completed",
      "is_error": false,
      "error": null,
      "human_query": null,
      "human_options": [],
      "allow_custom_response": true,
      "human_response": null,
      "execution_details": "precise instructions for executor"
    }
  }
}
"""

# Backward compatibility
PLANNER_PROMPT = PLANNER_CREATE_PROMPT

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


def _parse_task_response(message):
    """Parse task response from message format: 'task_id:task_1, answer:...'
    
    Returns: (task_id, answer) or (None, message)
    """
    if not message or not isinstance(message, str):
        return None, message
    
    # Check for format: "task_id:task_1, answer:..."
    if "task_id:" in message.lower() and "answer:" in message.lower():
        parts = message.split(",", 1)
        task_id = None
        answer = message
        
        for part in parts:
            part = part.strip()
            if part.lower().startswith("task_id:"):
                task_id = part.split(":", 1)[1].strip()
            elif part.lower().startswith("answer:"):
                answer = part.split(":", 1)[1].strip()
        
        return task_id, answer
    
    # No special format, return as-is
    return None, message


def _detect_update_type(user_input, existing_plan, task_id=None):
    """Detect whether to use delta update or full replan.
    
    Returns: "delta" | "full_replan"
    """
    if not existing_plan:
        return "create"
    
    # Case 1: Specific task_id provided (user responding to a task)
    if task_id:
        task = existing_plan.get("tasks", {}).get(task_id)
        if task and task.get("status") == "waiting_for_user":
            return "delta"  # Simple task response
    
    # Case 2: Check for full replan keywords
    replan_keywords = [
        "change the plan", "start over", "different approach",
        "actually i want", "instead of", "change goal",
        "add new", "remove task", "cancel"
    ]
    user_lower = (user_input or "").lower()
    if any(keyword in user_lower for keyword in replan_keywords):
        return "full_replan"
    
    # Case 3: Short response likely answering a question
    if len((user_input or "").split()) < 20:
        return "delta"
    
    # Case 4: Check if any task is waiting for user
    tasks = existing_plan.get("tasks", {})
    waiting_tasks = [tid for tid, t in tasks.items() if t.get("status") == "waiting_for_user"]
    if waiting_tasks:
        return "delta"  # Likely responding to waiting task
    
    # Default: use delta (safer, faster)
    return "delta"


def _get_dependency_context(plan, updated_task_id):
    """Build minimal context for delta update: affected task + dependencies.
    
    Returns dict with only relevant tasks for the update.
    """
    tasks = plan.get("tasks", {})
    if not tasks or updated_task_id not in tasks:
        return {}
    
    context = {}
    
    # 1. The task being updated
    context[updated_task_id] = tasks[updated_task_id]
    
    # 2. All tasks that depend on this task (direct dependents)
    for task_id, task in tasks.items():
        deps = task.get("dependencies", [])
        if updated_task_id in deps:
            context[task_id] = task
    
    # 3. Completed dependencies of the updated task (for context)
    updated_task_deps = tasks[updated_task_id].get("dependencies", [])
    for dep_id in updated_task_deps:
        if dep_id in tasks and tasks[dep_id].get("status") == "completed":
            # Include only result for completed dependencies
            context[dep_id] = {
                "title": tasks[dep_id].get("title"),
                "status": "completed",
                "result": tasks[dep_id].get("result")
            }
    
    return context


def _build_planner_message(
    user_goal,
    agent_context=None,
    existing_plan=None,
    user_feedback=None,
    user_system_prompt=None,
    update_type="create",
    delta_context=None,
):
    """Build the user message to send to the planner agent."""
    parts = []

    if user_system_prompt:
        parts.append(f"## USER AGENT SYSTEM PROMPT\n{user_system_prompt}")

    if update_type == "delta" and delta_context:
        # Delta update: send only affected tasks
        delta_json = json.dumps(delta_context, indent=2, default=str)
        parts.append(f"\n## AFFECTED TASKS (update these only)\n{delta_json}")
        parts.append(f"\n## User's Response\n{user_feedback or 'None'}")
        parts.append(f"\n## Original Goal\n{existing_plan.get('goal', '')}")
    
    elif existing_plan:
        # Full replan: send entire plan
        plan_json = json.dumps(existing_plan, indent=2, default=str)
        parts.append(f"\n## CURRENT PLAN\n{plan_json}")
        parts.append(f"\n## User's Request\n{user_feedback or 'None'}")
     
    else:
        # New plan creation
        parts.append(f"## User's Request\n{user_goal}")

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

    # Parse message format: "task_id:task_1, answer:..."
    user_input = parsed_data.get("user", "")
    parsed_task_id, parsed_answer = _parse_task_response(user_input)
    
    # Use parsed task_id if found, otherwise check parsed_data
    task_id = parsed_task_id or parsed_data.get("task_id")
    
    # Use parsed answer if found, otherwise use original user_input
    if parsed_task_id and parsed_answer:
        user_input = parsed_answer
        logger.info(f"Parsed task response: task_id={task_id}, answer={parsed_answer[:50]}...")
    
    # Detect update type and select appropriate prompt
    update_type = _detect_update_type(user_input, existing_plan, task_id)
    
    # Select prompt based on update type
    if update_type == "create":
        default_prompt = PLANNER_CREATE_PROMPT
    elif update_type == "delta":
        default_prompt = PLANNER_DELTA_UPDATE_PROMPT
    else:  # full_replan
        default_prompt = PLANNER_FULL_UPDATE_PROMPT
    
    # Try to get custom prompt from DB, fallback to default
    db_planner_prompt = await _get_planner_prompt_from_db(default_prompt)
    
    # Build agent context and system prompt
    agent_context = _build_agent_context(parsed_data, bridge_configurations)
    planner_prompt = _build_planner_system_prompt(db_planner_prompt, agent_context, session_memory)
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    parsed_data.setdefault("configuration", {})["prompt"] = planner_prompt
    
    # Remove tool calling capability from planner - tools are only for executor
    parsed_data["configuration"]["tools"] = []

    custom_config["response_type"] = {"type": "json_object"}

    # Build user message based on update type
    if update_type == "delta":
        # Delta update: send only affected tasks
        # Find the task being updated (either from task_id or first waiting_for_user)
        if not task_id:
            waiting_tasks = [
                tid for tid, t in existing_plan.get("tasks", {}).items()
                if t.get("status") == "waiting_for_user"
            ]
            task_id = waiting_tasks[0] if waiting_tasks else None
        
        delta_context = _get_dependency_context(existing_plan, task_id) if task_id else {}
        
        parsed_data["user"] = _build_planner_message(
            user_goal=existing_plan.get("goal") if existing_plan else user_input,
            user_feedback=user_input,
            user_system_prompt=original_prompt,
            existing_plan=existing_plan,
            update_type="delta",
            delta_context=delta_context,
        )
        
        logger.info(f"Delta update for task {task_id}: sending {len(delta_context)} tasks instead of {len(existing_plan.get('tasks', {}))}")
    
    elif existing_plan:
        # Full replan: send entire plan
        parsed_data["user"] = _build_planner_message(
            user_goal=existing_plan.get("goal"),
            user_feedback=user_input,
            user_system_prompt=original_prompt,
            existing_plan=existing_plan,
            update_type="full_replan",
        )
        
        logger.info(f"Full replan: sending all {len(existing_plan.get('tasks', {}))} tasks")
    
    else:
        # First-time plan creation
        parsed_data["user"] = _build_planner_message(
            user_goal=user_input,
            user_system_prompt=original_prompt,
            update_type="create",
        )
        
        logger.info("Creating new plan from scratch")


