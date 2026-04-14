import json

from globals import logger
from src.configs.constant import service_name
from src.services.todo import plan_store

from src.services.commonServices.openAI.runModel import openai_response_stream
from src.services.commonServices.anthropic.anthropicModelRun import anthropic_stream
from src.services.commonServices.groq.groqModelRun import groq_stream
from src.services.commonServices.grok.grokModelRun import grok_stream
from src.services.commonServices.Google.gemini_modelrun import gemini_modelrun_stream
from src.services.commonServices.Mistral.mistral_model_run import mistral_stream
from src.services.commonServices.openRouter.openRouter_modelrun import openrouter_stream
from src.services.commonServices.baseService.utils import run_stream_and_collect

PLANNER_PROMPT = """You are a task planner agent. Your job is to break down a user's goal into a structured plan of tasks.

You will receive:
- The user's goal
- The main agent's capabilities (system prompt, tools, connected agents)
- Optionally, an existing plan and user feedback to update it

Rules:
1. **IMPORTANT - Simplicity First**: For simple, straightforward goals, create a SINGLE task. Only break into multiple tasks when:
   - The goal genuinely requires multiple distinct steps that depend on each other
   - Different agents/tools are needed for different parts
   - Parallel execution would provide real value
   - The goal is complex and would benefit from granular tracking
   Examples of single-task goals: "write a function", "analyze this data", "fix this bug", "summarize this document"
   Examples of multi-task goals: "build and deploy application", "research, analyze, and present findings", "setup infrastructure across multiple services"

2. Each task should be a clear, actionable unit of work with a specific outcome.

3. Use the assigned_agent field to delegate tasks to specific connected agents when appropriate. Use their bridge_id.

4. Use the assigned_tool field when a specific tool should be used for the task.

5. Set dependencies between tasks using task_id references (e.g. ["task_1"]). Tasks with no dependencies can run in parallel.

6. Set max_retry to 2 for tasks that might fail transiently, 0 for tasks that should not retry.

7. **Human-in-the-Loop - Ask ONLY When Necessary**: Create tasks with status "waiting_for_user" ONLY when:
   - Critical information is genuinely missing and you cannot proceed (e.g., API keys, server URLs, database credentials)
   - The goal is ambiguous in a way that could lead to completely different outcomes (e.g., "deploy" without knowing staging vs production)
   - Destructive operations need explicit approval (e.g., deleting data, dropping databases)
   - User needs to choose between fundamentally different technical approaches
   
   DO NOT ask for:
   - Information you can infer or use reasonable defaults for
   - Trivial preferences (naming, colors, formatting) - make reasonable choices
   - Details that can be determined or adjusted later
   - Confirmation for routine, safe operations

8. When you create a waiting_for_user task:
   - Set human_query with a clear, specific question
   - Set dependencies so this task blocks tasks that need the answer
   - Make the question focused and actionable

Respond with ONLY a valid JSON object. No markdown, no explanation, just the JSON."""

PLAN_JSON_SCHEMA = {
    "goal": "string - the user's original goal",
    "tasks": {
        "task_1": {
            "title": "short title",
            "task_description": "detailed description of what this task should accomplish",
            "status": "pending | waiting_for_user - use 'waiting_for_user' if you need user input before this task can execute",
            "dependencies": ["array of task_ids that must complete before this task can start"],
            "assigned_agent": "bridge_id of the agent to handle this task, or null for the main agent",
            "assigned_tool": "tool_name if a specific tool should be used, or null",
            "retry": 0,
            "max_retry": 2,
            "result": None,
            "is_error": False,
            "error": None,
            "human_query": "if status is 'waiting_for_user', put your question here (clear, specific question)",
            "human_response": None,
        }
    },
}

# Streaming functions per service — all take (configuration, apikey) positional args
STREAM_FUNCTIONS = {
    service_name["openai"]: openai_response_stream,
    service_name["anthropic"]: anthropic_stream,
    service_name["groq"]: groq_stream,
    service_name["grok"]: grok_stream,
    service_name["gemini"]: gemini_modelrun_stream,
    service_name["mistral"]: mistral_stream,
    service_name["open_router"]: openrouter_stream,
}

# These services need stream=True added to the config dict (SDK-based, not handled internally)
_NEEDS_STREAM_FLAG = {service_name["groq"], service_name["open_router"]}


def _build_agent_context(parsed_data, bridge_configurations):
    """Build a context string describing the available agents and tools for the planner."""
    main_bridge_id = parsed_data["bridge_id"]
    main_config = bridge_configurations.get(main_bridge_id, {})

    context_parts = []

    # Main agent info
    main_prompt = main_config.get("configuration", {}).get("prompt", "")
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

    parts.append(f"\nRespond with ONLY a valid JSON object matching this schema:\n{json.dumps(PLAN_JSON_SCHEMA, indent=2)}")
    parts.append("\nIMPORTANT: Use actual bridge_ids from the context above for assigned_agent. "
                 "Set dependencies as task_id references (e.g. [\"task_1\"]). "
                 "Tasks with no dependencies can run in parallel.")

    return "\n".join(parts)


def _build_llm_config(model, service, planner_message):
    """Build a minimal streaming LLM configuration."""
    if service == service_name["anthropic"]:
        return {
            "model": model,
            "system": PLANNER_PROMPT,
            "messages": [{"role": "user", "content": planner_message}],
            "max_tokens": 4096,
        }
    elif service == service_name["gemini"]:
        return {
            "model": model,
            "contents": [
                {"role": "user", "parts": [{"text": PLANNER_PROMPT + "\n\n" + planner_message}]},
            ],
        }
    elif service == service_name["openai"]:
        # OpenAI Responses API: stream=True added internally by openai_response_stream
        return {
            "model": model,
            "instructions": PLANNER_PROMPT,
            "input": planner_message,
        }
    else:
        # Groq, Grok, Mistral, OpenRouter — Chat Completions format
        config = {
            "model": model,
            "messages": [
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": planner_message},
            ],
        }
        # Groq and OpenRouter SDK need stream=True in the config
        if service in _NEEDS_STREAM_FLAG:
            config["stream"] = True
        return config


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


async def _call_planner_streaming(planner_message, parsed_data, bridge_configurations, streamer):
    """
    Call the LLM in streaming mode. Tokens are emitted to `streamer` as they arrive.
    Returns the parsed plan JSON dict after the stream completes.
    """
    main_bridge_id = parsed_data["bridge_id"]
    main_config = bridge_configurations.get(main_bridge_id, {})

    model = main_config.get("configuration", {}).get("model", parsed_data.get("model"))
    service = main_config.get("service", parsed_data.get("service"))
    apikey = main_config.get("apikey", parsed_data.get("apikey"))

    stream_fn = STREAM_FUNCTIONS.get(service)
    if not stream_fn:
        raise ValueError(f"Unsupported service for planner: {service}")

    config = _build_llm_config(model, service, planner_message)

    # All stream functions take (configuration, apikey) as positional args
    generator = stream_fn(config, apikey)
    stream_state = await run_stream_and_collect(generator, streamer)

    if stream_state.get("error_in_stream"):
        raise ValueError(f"Planner stream error: {stream_state['error_in_stream']}")

    content = "".join(stream_state.get("accumulated_content", []))
    if not content:
        raise ValueError("Planner returned empty response")

    return _parse_plan_json(content)


async def create_plan(parsed_data, bridge_configurations, streamer):
    """Create a new plan, streaming tokens to `streamer`."""
    user_goal = parsed_data["user"]
    agent_context = _build_agent_context(parsed_data, bridge_configurations)
    planner_message = _build_planner_message(user_goal, agent_context)

    plan_data = await _call_planner_streaming(planner_message, parsed_data, bridge_configurations, streamer)

    plan = {
        "goal": plan_data.get("goal", user_goal),
        "state": "planning",
        "bridge_id": parsed_data["bridge_id"],
        "org_id": parsed_data["org_id"],
        "thread_id": parsed_data["thread_id"],
        "sub_thread_id": parsed_data.get("sub_thread_id") or parsed_data["thread_id"],
        "tasks": plan_data.get("tasks", {}),
    }

    await plan_store.save_plan(plan)
    return plan


async def update_plan(existing_plan, user_feedback, parsed_data, bridge_configurations, streamer):
    """Update an existing plan based on user feedback, streaming tokens to `streamer`."""
    agent_context = _build_agent_context(parsed_data, bridge_configurations)
    planner_message = _build_planner_message(
        existing_plan["goal"],
        agent_context,
        existing_plan=existing_plan,
        user_feedback=user_feedback,
    )

    plan_data = await _call_planner_streaming(planner_message, parsed_data, bridge_configurations, streamer)

    existing_plan["tasks"] = plan_data.get("tasks", existing_plan["tasks"])
    existing_plan["goal"] = plan_data.get("goal", existing_plan["goal"])
    existing_plan["state"] = "planning"

    await plan_store.update_plan(existing_plan)
    return existing_plan
