import json
import re

from globals import logger
from src.services.todo import plan_store


def _has_task_ids_in_message(user_message):
    if not user_message:
        return False
    return bool(re.compile(r'task_id\s*:\s*task_\d+', re.IGNORECASE).search(user_message))


def _has_question_ids_in_message(user_message):
    if not user_message:
        return False
    return bool(re.compile(r'question_id\s*:\s*q\d+', re.IGNORECASE).search(user_message))


def _extract_question_answer_pairs(user_message):
    """Extract question_id→answer mapping from a structured human-loop reply.

    Expected format: 'question_id:q1, answer:Gmail\\nquestion_id:q2, answer:Every email'
    Falls back to {} when the message is free-form (no structured pairs found).
    """
    if not user_message:
        return {}
    pattern = re.compile(
        r'question_id\s*:\s*(q\d+)\s*,\s*answer\s*:\s*([^\n]+?)(?=\s*question_id\s*:|$)',
        re.IGNORECASE | re.DOTALL,
    )
    return {q_id.strip(): ans.strip() for q_id, ans in pattern.findall(user_message)}


_AGENT_TOOL_SYNTHETIC_KEYS = {"_query", "action_type"}


def _tool_name(tool):
    return tool.get("name") or (tool.get("function") or {}).get("name", "")


def _extract_agent_real_params(agent_tool, static_field_keys=None):
    """Return the AGENT tool's AI-generated params only.

    Drops:
      - synthetic keys (_query, action_type) added by add_connected_agents
      - static fields filled by the gateway from variables_path[bridge_id]
    Returns (fields, required).
    """
    static_field_keys = set(static_field_keys or ())
    skip = _AGENT_TOOL_SYNTHETIC_KEYS | static_field_keys
    props = agent_tool.get("properties") or {}
    required = agent_tool.get("required") or []
    fields = {k: v for k, v in props.items() if k not in skip}
    real_required = [r for r in required if r not in skip]
    return fields, real_required


def _split_agent_and_non_agent_tools(tools, tool_id_and_name_mapping):
    """Partition tools into ({bridge_id: agent_tool}, non_agent_tools).

    Connected agents (mapping type == 'AGENT') get keyed by bridge_id so the
    planner context can attach each agent's real param schema to its entry.
    """
    if not tool_id_and_name_mapping:
        return {}, tools
    agent_tools_by_bridge = {}
    non_agent_tools = []
    for tool in tools:
        mapping = tool_id_and_name_mapping.get(_tool_name(tool)) or {}
        if mapping.get("type") == "AGENT":
            bid = mapping.get("bridge_id")
            if bid:
                agent_tools_by_bridge[bid] = tool
        else:
            non_agent_tools.append(tool)
    return agent_tools_by_bridge, non_agent_tools


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


_PLANNER_VALUE_PREVIEW_LIMIT = 400


def _preview_variable_value(value):
    """Render a variable value for the planner context — JSON for structured
    values, str() for scalars, truncated to keep the system prompt small."""
    if isinstance(value, (dict, list)):
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(value)
    else:
        rendered = str(value)
    if len(rendered) > _PLANNER_VALUE_PREVIEW_LIMIT:
        rendered = rendered[:_PLANNER_VALUE_PREVIEW_LIMIT] + "…"
    return rendered


def _collect_path_targeted_var_keys(variables_path):
    """Return the set of top-level parent-variable keys that are already wired
    via variables_path (any child agent). The gateway auto-fills these into
    each child's args from parent variables — the planner does NOT need to
    surface them in agent_variables."""
    path_keys = set()
    if not isinstance(variables_path, dict):
        return path_keys
    for per_agent in variables_path.values():
        if not isinstance(per_agent, dict):
            continue
        for parent_path in per_agent.values():
            if isinstance(parent_path, str) and parent_path:
                # lodash-style "a.b.c" — top-level key is what binds to
                # parent variables dict.
                path_keys.add(parent_path.split(".", 1)[0])
    return path_keys


def _build_non_path_variables_block(parsed_data):
    """Show ONLY the parent variables that are NOT already wired via any
    variables_path mapping. Path-mapped ones are auto-filled by the gateway;
    the remaining ones are what the planner must surface in each connected
    agent task's `agent_variables`."""
    variables = parsed_data.get("variables") or {}
    if not isinstance(variables, dict) or not variables:
        return ""
    path_keys = _collect_path_targeted_var_keys(parsed_data.get("variables_path") or {})
    non_path_entries = [
        (k, v) for k, v in variables.items()
        if k not in path_keys and k not in ("user", "_query")
    ]
    if not non_path_entries:
        return ""
    lines = [
        "\nAvailable parent variables — these are NOT auto-mapped by the gateway, "
        "so when a connected agent task needs one of these values you MUST copy it "
        "into the task's `agent_variables`. Use the literal values shown:"
    ]
    for var_name, var_value in non_path_entries:
        lines.append(f"  - {var_name}: {_preview_variable_value(var_value)}")
    return "\n".join(lines)


def _format_field_for_planner(field_name, field_spec, is_required):
    """Render a single AGENT/tool field for the planner context — name, type,
    required flag, description, enum — so the planner can produce a concrete
    value for it at plan time."""
    if not isinstance(field_spec, dict):
        return f"      - {field_name}{' (required)' if is_required else ''}"
    ftype = field_spec.get("type", "string")
    fdesc = (field_spec.get("description") or "").strip()
    fenum = field_spec.get("enum") or []
    req_marker = " (required)" if is_required else ""
    line = f"      - {field_name} [{ftype}]{req_marker}"
    if fdesc:
        line += f": {fdesc}"
    if fenum:
        line += f" — allowed: {list(fenum)}"
    return line


def _build_agent_context(parsed_data, bridge_configurations, other_tools=None, agent_tools_by_bridge=None):
    main_bridge_id = parsed_data["bridge_id"]
    agent_tools_by_bridge = agent_tools_by_bridge or {}
    variables_path = parsed_data.get("variables_path") or {}
    context_parts = []

    connected_agents = []
    for bid, config in bridge_configurations.items():
        if bid == main_bridge_id:
            continue
        agent_name = config.get("name", bid)
        bridge_summary = config.get("bridge_summary") or ""
        agent_tool = agent_tools_by_bridge.get(bid)
        if agent_tool:
            static_keys = (variables_path.get(bid) or {}).keys()
            fields, required = _extract_agent_real_params(agent_tool, static_keys)
        else:
            fields, required = {}, []
        connected_agents.append(
            f"  - Agent Name: {agent_name} | Agent Id: {bid} | Summary: {bridge_summary}"
        )
        if fields:
            connected_agents.append(
                "    agent_variables you MUST emit for this agent's tasks "
                "(path-mapped params are already auto-filled by the gateway and are NOT listed here):"
            )
            for fname, fspec in fields.items():
                connected_agents.append(
                    _format_field_for_planner(fname, fspec, fname in (required or []))
                )

    if connected_agents:
        context_parts.append(
            "Connected Agents (when assigning a task to one, set `assigned_agent` "
            "to an OBJECT {agent_id, agent_variables} — use the Agent Id below, NOT the Agent Name):"
        )
        context_parts.extend(connected_agents)

    non_path_vars_block = _build_non_path_variables_block(parsed_data)
    if non_path_vars_block:
        context_parts.append(non_path_vars_block)

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
    qa_history=None,
    original_goal=None,
):
    """Build the user message for the planner agent."""
    parts = []

    if existing_plan:
        # Surface the original goal so the planner never loses context of what
        # the user actually wanted — critical when plan is null (info-gathering).
        if original_goal:
            parts.append(f"\nOriginal user goal: {original_goal}")

        # Inject answered Q&A so the planner knows what has already been resolved.
        # This prevents the AI from re-asking questions the user already answered
        # across multiple human-loop turns.
        answered_qa = [
            e for e in (qa_history or [])
            if e.get("answer") not in (None, "")
        ]
        if answered_qa:
            parts.append(
                "\nPreviously answered questions — these are RESOLVED, do NOT ask them again:"
            )
            for entry in answered_qa:
                parts.append(f"  Q: {entry['question']}")
                parts.append(f"  A: {entry['answer']}")

        if existing_plan.get("questions"):
            parts.append("\nQuestions:")
            parts.append(json.dumps(existing_plan["questions"], indent=2, default=str))

        if user_feedback:
            parts.append(f"\nUser Message: {user_feedback}")

        if is_human_loop:
            parts.append(
                "\nThe user has provided answers to pending task questions. "
                "Process these answers and continue with the next steps."
            )

        if is_question_loop:
            parts.append("\nMark answered questions as 'answered' and continue planning. Do not regenerate the full plan.")
    else:
        parts.append(user_goal)

    return "\n".join(parts)


def _build_planner_system_prompt(agent_context, existing_plan=None, user_system_prompt=None):
    system_prompt_parts = []

    system_prompt_parts.append(f"\nAvailable Agents and Tools:\n{agent_context}")

    if existing_plan and existing_plan.get("plan"):
        system_prompt_parts.append(
            "\nPreviously built plan by you (AI):\n"
            + json.dumps(existing_plan["plan"], indent=2, default=str)
        )

    system_prompt_content = "\n".join(system_prompt_parts)
    return (user_system_prompt or "") + "\n" + system_prompt_content


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


async def prepare_planner_request(parsed_data, bridge_configurations, custom_config):
    org_id = parsed_data["org_id"]
    bridge_id = parsed_data["bridge_id"]
    thread_id = parsed_data["thread_id"]
    sub_thread_id = parsed_data.get("sub_thread_id") or thread_id

    existing_plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)

    user_input = parsed_data.get("user", "")
    has_task_ids = _has_task_ids_in_message(user_input)
    has_question_ids = _has_question_ids_in_message(user_input)

    original_tools = parsed_data.get("configuration", {}).get("tools", [])
    tool_id_and_name_mapping = parsed_data.get("tool_id_and_name_mapping", {})
    agent_tools_by_bridge, non_agent_tools = _split_agent_and_non_agent_tools(
        original_tools, tool_id_and_name_mapping
    )
    search_tools, other_tools = _separate_search_and_other_tools(non_agent_tools)

    parsed_data.setdefault("configuration", {})["tools"] = search_tools
    if search_tools:
        custom_config["tools"] = search_tools
    else:
        custom_config.pop("tools", None)

    # First call — no existing plan — save the user's goal so it survives
    # info-gathering rounds where plan is still null.
    if not existing_plan and user_input:
        await plan_store.save_planner_goal(org_id, bridge_id, thread_id, sub_thread_id, user_input)

    # When the user is answering planner-level questions, persist those Q&A pairs
    # to the session so subsequent planner calls can see what was already resolved.
    if existing_plan and (has_task_ids or has_question_ids) and user_input:
        # Try to extract precise per-question answers first (question_id:q1, answer:...)
        qid_to_answer = _extract_question_answer_pairs(user_input)
        for q in (existing_plan.get("questions") or []):
            if not isinstance(q, dict):
                continue
            q_text = q.get("question", "")
            if not q_text:
                continue
            # Use the mapped answer when available, fall back to the full user message
            answer = qid_to_answer.get(q.get("id", "")) or user_input
            await plan_store.add_to_planner_session(
                org_id, bridge_id, thread_id, sub_thread_id, q_text, answer,
            )

    # Load full Q&A history and original goal so the planner message can surface
    # already-answered questions and the original intent on every re-invocation.
    session = await plan_store.get_planner_session(org_id, bridge_id, thread_id, sub_thread_id)
    qa_history = session.get("qa_history") or []
    # Goal resolution order:
    #   1. top-level goal the planner emits in its JSON (now part of schema)
    #   2. goal inside plan.goal (set when plan is not null)
    #   3. session-stored goal (saved on first call, fallback for old sessions)
    original_goal = (
        (existing_plan or {}).get("goal")
        or ((existing_plan or {}).get("plan") or {}).get("goal")
        or session.get("goal")
        or ""
    )

    conversation = []
    if existing_plan and existing_plan.get("history_summary"):
        history_summary = existing_plan["history_summary"]
        if not isinstance(history_summary, str):
            history_summary = json.dumps(history_summary)
        conversation = [{"role": "assistant", "content": history_summary}]
    parsed_data.setdefault("configuration", {})["conversation"] = conversation

    agent_context = _build_agent_context(
        parsed_data, bridge_configurations, other_tools, agent_tools_by_bridge,
    )
    original_prompt = (parsed_data.get("configuration") or {}).get("prompt") or ""
    planner_prompt = _build_planner_system_prompt(agent_context, existing_plan, original_prompt)
    parsed_data.setdefault("configuration", {})["prompt"] = planner_prompt

    custom_config["response_type"] = {"type": "json_object"}

    if existing_plan:
        parsed_data["user"] = _build_planner_message(
            user_goal=original_goal,
            existing_plan=existing_plan,
            user_feedback=user_input,
            is_human_loop=has_task_ids,
            is_question_loop=has_question_ids,
            qa_history=qa_history,
            original_goal=original_goal,
        )
    else:
        parsed_data["user"] = _build_planner_message(user_goal=user_input)
