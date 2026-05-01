import asyncio
import copy
import json
import time
import uuid

from globals import logger
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_without_org_service
from src.services.todo import plan_store


TERMINAL_STATUSES = {"completed", "failed", "skipped", "waiting_for_user"}
# Statuses that should be preserved across replans (the planner is not allowed
# to delete these). `needs_replan` is intentionally NOT preserved — it's the
# planner's job to resolve those tasks on the next revision.
PROTECTED_STATUSES = {"completed"}
REPLAN_PRESERVE_STATUSES = {"completed", "pending", "in_progress", "waiting_for_user", "failed"}
INTERRUPTED_TASK_RECOVERY_NOTE = "Task was in_progress when execution was interrupted; reset to pending for retry"

WORKER_RESPONSE_SCHEMA = """
{
  "status": "completed | waiting_for_user | failed",
  "result": "tool output or extracted answer if completed, else null",
  "reasoning": "1 sentence: what you did or why you couldn't",
  "human_query": "question for user if waiting_for_user, else null",
  "error": "error description if failed, else null"
}"""

WORKER_SYSTEM_PROMPT_TEMPLATE = """You are the EXECUTOR worker for ONE task in a Planner -> Executor pipeline.

Your default behavior: understand the task, prepare correct arguments, then call the assigned tool immediately.

Task title: {title}
Task goal: {task_description}

## Execution details from planner
{execution_details}

## Per-task instructions
{task_prompt}

{human_response_block}## Rules
Tool(s) available: {tool_list}

1. Before calling, quickly verify: task goal, required fields, and value source (execution details, dependency results, user clarification).
2. Use values from execution details exactly as given; do not invent missing required values.
3. Call the assigned tool without unnecessary delay once arguments are prepared.
4. If the tool errors, analyze the error, correct arguments if possible, and retry up to 2 times.
5. If still not solvable or missing required information, return `waiting_for_user` with a clear `human_query` asking for help.
6. Use `failed` only for unrecoverable infrastructure issues.

Decision policy based on tool result:
- Return `completed` when the tool returns a successful and usable result.
- Return `waiting_for_user` when you need user help: missing inputs, unclear requirements, permission issues, or any problem you cannot resolve. Ask a clear question in `human_query`.
- Return `failed` only when the system is unavailable after retries (service down, transport outage, repeated timeout/rate-limit).

## Response format
Return only one valid JSON object (no markdown, no extra text):
{schema}

Status guide:
- "completed": task finished successfully.
- "waiting_for_user": need user help to proceed; include a clear question in `human_query`.
- "failed": unrecoverable infrastructure failure."""


_WORKER_PROMPT_TEMPLATE_CACHE = None
_WORKER_PROMPT_TEMPLATE_FETCHED = False
_WORKER_PROMPT_TEMPLATE_LOCK = asyncio.Lock()


async def _get_worker_prompt_template_from_db(default_prompt):
    """Fetch worker prompt template from DB only once per process.

    Mirrors planner prompt override behavior while avoiding repeated DB reads.
    """
    global _WORKER_PROMPT_TEMPLATE_CACHE, _WORKER_PROMPT_TEMPLATE_FETCHED

    if _WORKER_PROMPT_TEMPLATE_FETCHED:
        return _WORKER_PROMPT_TEMPLATE_CACHE or default_prompt

    async with _WORKER_PROMPT_TEMPLATE_LOCK:
        if _WORKER_PROMPT_TEMPLATE_FETCHED:
            return _WORKER_PROMPT_TEMPLATE_CACHE or default_prompt

        try:
            prompt_data = await get_specific_prebuilt_prompt_without_org_service("worker_prompt")
            prompt_override = (prompt_data or {}).get("worker_prompt")
            if isinstance(prompt_override, str) and prompt_override.strip():
                _WORKER_PROMPT_TEMPLATE_CACHE = prompt_override
        except Exception as err:
            logger.error(f"Error fetching worker_prompt from preBuiltPrompts: {err}")
        finally:
            _WORKER_PROMPT_TEMPLATE_FETCHED = True

    return _WORKER_PROMPT_TEMPLATE_CACHE or default_prompt


def _filter_tools_for_task(agent_config, task_tool_names):
    """Return the subset of the agent's configured tools whose name is in
    `task_tool_names`. If `task_tool_names` is None/missing -> no tools
    (executor should not perform research; planner already did).
    Accepts a single string (single assigned tool) or a list of names.
    Falls back to case-insensitive matching when an exact match fails so the
    worker isn't blocked by the planner capitalizing a tool name.
    """
    all_tools = ((agent_config or {}).get("configuration") or {}).get("tools") or []
    if task_tool_names is None or task_tool_names == "":
        return []
    if isinstance(task_tool_names, str):
        allow = {task_tool_names}
    else:
        allow = set(task_tool_names)

    def _tool_name(tool):
        return tool.get("name") or (tool.get("function") or {}).get("name")

    # Exact match first.
    filtered = [t for t in all_tools if _tool_name(t) in allow]
    if filtered:
        return filtered

    # Case-insensitive fallback.
    allow_lower = {a.lower() for a in allow if isinstance(a, str)}
    ci_filtered = [t for t in all_tools if (_tool_name(t) or "").lower() in allow_lower]
    if ci_filtered:
        matched = [_tool_name(t) for t in ci_filtered]
        logger.warning(
            f"Tool filter matched case-insensitively: requested={sorted(allow)} matched={matched}"
        )
        return ci_filtered

    available = [_tool_name(t) for t in all_tools if _tool_name(t)]
    logger.error(
        f"Tool filter found NO match. Requested={sorted(allow)}. "
        f"Available on agent={available}."
    )
    return []


def _build_dependency_context(task, all_tasks):
    """Build context from completed dependency tasks."""
    dependencies = task.get("dependencies", [])
    if not dependencies:
        return ""
    
    context_parts = ["DEPENDENCY_RESULTS\n"]
    context_parts.append("Use these completed task results when execution_details references them:\n")
    
    for dep_id in dependencies:
        dep_task = all_tasks.get(dep_id)
        if not dep_task or dep_task.get("status") != "completed":
            continue
        
        dep_title = dep_task.get("title", dep_id)
        dep_result = dep_task.get("result", "No result")
        context_parts.append(f"- {dep_id} ({dep_title}):")
        context_parts.append(f"  Result: {dep_result}\n")
    
    return "\n".join(context_parts) if len(context_parts) > 2 else ""


def _build_worker_system_prompt(task, filtered_tool_names, all_tasks=None, prompt_template=WORKER_SYSTEM_PROMPT_TEMPLATE):
    """Compose the worker's system prompt. execution_details carries the exact
    tool-invocation payload resolved by the planner; task_description and
    task_prompt give the natural-language goal; dependency results and prior
    human_response are injected as additional context.
    """
    def _as_text(value, default=""):
        """Coerce a possibly-dict/list value into a string for prompt embedding."""
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, indent=2, default=str)
        except Exception:
            return str(value)

    task_prompt = _as_text(task.get("worker_task") or task.get("task_description"))
    execution_details = _as_text(
        task.get("execution_details"),
        default="(none — use task_description and tool schema)",
    )
    tool_list = ", ".join(filtered_tool_names) if filtered_tool_names else "none"

    dependency_context = _build_dependency_context(task, all_tasks or {})
    dependency_block = f"{dependency_context}\n" if dependency_context else ""

    human_response = task.get("human_response")
    human_response_block = (
        f"USER_CLARIFICATION_ANSWER\n{human_response}\n\n"
        if human_response
        else ""
    )

    format_kwargs = {
        "title": task.get("title", ""),
        "task_description": task.get("task_description", ""),
        "execution_details": execution_details,
        "task_prompt": task_prompt,
        "human_response_block": human_response_block,
        "tool_list": tool_list,
        "schema": WORKER_RESPONSE_SCHEMA,
    }

    # Build the full prompt with dependency context.
    # If a DB-managed template is malformed/missing placeholders, fall back
    # to the default worker template so task execution keeps working.
    try:
        base_prompt = prompt_template.format(**format_kwargs)
    except Exception as err:
        logger.error(f"Error formatting worker prompt template, using default template: {err}")
        base_prompt = WORKER_SYSTEM_PROMPT_TEMPLATE.format(**format_kwargs)
    
    # Return the complete prompt with dependency context if available
    if dependency_context:
        return f"{base_prompt}\n\n{dependency_block}"
    return base_prompt


def _build_worker_user_message(task_id, task):
    """Concise user message to pair with system prompt for the worker call."""
    return (
        f"Execute task {task_id}: {task.get('title', '')}. "
        "Call the assigned tool/agent now using execution_details and return JSON only."
    )


def _build_worker_result(parsed):
    """Shape a parsed worker response into the dict execute_plan expects.

    Workers can now directly request user input via waiting_for_user status.
    `success` is False only for status=failed so the retry path kicks in;
    waiting_for_user is handled as success with a status that pauses execution.
    """
    status = parsed.get("status") or "completed"
    if status == "needs_planner":
        status = "waiting_for_user"
    if status not in {"completed", "waiting_for_user", "failed"}:
        status = "waiting_for_user"
    return {
        "success": status != "failed",
        "status": status,
        "result": parsed.get("result"),
        "reasoning": parsed.get("reasoning"),
        "human_query": parsed.get("human_query") or parsed.get("replan_reason"),
        "error": parsed.get("error"),
    }


def _parse_worker_response(content):
    """Parse a worker's JSON reply. Strips markdown fences like _parse_plan_json.
    On failure, ask user for help instead of trying to auto-fix.
    """
    if not content:
        return {"status": "waiting_for_user", "human_query": "The worker did not return a response. Please try again or provide more details."}
    raw = content
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "status" not in parsed:
            raise ValueError("missing status field")
        return parsed
    except (json.JSONDecodeError, ValueError) as err:
        logger.warning(f"Worker response was not valid JSON, asking user for help: {err}")
        return {
            "status": "waiting_for_user",
            "result": None,
            "human_query": (
                "The task encountered an error and couldn't complete. "
                f"Error: {str(err)[:200]}. Please review and provide guidance."
            ),
        }


def _init_main_agent_metrics():
    """
    Aggregate container for main-agent task telemetry. Primary-agent sub-tasks
    run with skip_history=True, so nothing else persists their tokens/latency/
    tools/reasoning. We sum them here and hand them to the final history update.
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "expected_cost": 0.0,
        "latency_total": 0.0,
        "per_task_latency": [],
        "tools_call_data": [],
        "_tool_calls_by_id": {},
        "reasoning_parts": [],
        "service": None,
        "model": None,
        "finish_reason": "stop",
        "success": True,
        "firstAttemptError": None,
        "AiConfig": None,
        "fallback_model": None,
        "llm_urls": [],
        "annotations": [],
        "last_error": None,
    }


def _merge_task_metrics(
    metrics,
    task_id,
    done_event,
    collected_tool_calls,
    reasoning_buf,
    task_success,
    error,
    *,
    agent_config=None,
    elapsed_seconds=None,
    fallback_model=None,
    service=None,
    model=None,
):
    """Fold a single primary-agent task's telemetry into the shared aggregate.

    `agent_config`, `fallback_model`, `service`, `model`, `elapsed_seconds`
    come from the executor context (bridge_configurations + wall-clock timing)
    because the inner chat's SSE `done` event only carries `response.data`
    and `response.usage`.  AiConfig / fallback_model / latency / firstAttemptError
    live in `result.historyParams` inside baseService and are never streamed.
    """
    if metrics is None:
        return

    usage = (done_event or {}).get("usage") or {}
    response = (done_event or {}).get("response") or {}
    data = response.get("data") or {}

    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    total_t = usage.get("total_tokens") or (input_t + output_t)
    cost = usage.get("cost") or usage.get("expected_cost") or 0

    metrics["input_tokens"] += input_t
    metrics["output_tokens"] += output_t
    metrics["total_tokens"] += total_t
    try:
        metrics["expected_cost"] += float(cost or 0)
    except (TypeError, ValueError):
        pass

    # Latency: prefer the provider-reported figure, fall back to wall-clock.
    provider_latency = data.get("latency")
    if isinstance(provider_latency, dict):
        over_all = provider_latency.get("over_all_time") or 0
    elif isinstance(provider_latency, (int, float)):
        over_all = provider_latency
    else:
        over_all = 0
    try:
        over_all = float(over_all or 0)
    except (TypeError, ValueError):
        over_all = 0.0
    if over_all <= 0 and elapsed_seconds is not None:
        try:
            over_all = float(elapsed_seconds)
        except (TypeError, ValueError):
            over_all = 0.0
    metrics["latency_total"] += over_all
    metrics["per_task_latency"].append({"task_id": task_id, "time": round(over_all, 4)})

    effective_model = data.get("model") or model
    if effective_model and not metrics["model"]:
        metrics["model"] = effective_model
    effective_service = response.get("service") or service
    if effective_service and not metrics["service"]:
        metrics["service"] = effective_service

    if response.get("firstAttemptError"):
        metrics["firstAttemptError"] = response["firstAttemptError"]

    # AiConfig = the bridge's customConfig sent to the LLM.  Pulled from the
    # bridge configuration we already have in hand.
    if agent_config and not metrics["AiConfig"]:
        metrics["AiConfig"] = agent_config

    if fallback_model and not metrics["fallback_model"]:
        metrics["fallback_model"] = fallback_model

    if response.get("llm_urls"):
        metrics["llm_urls"].extend(response["llm_urls"])
    if response.get("annotations"):
        metrics["annotations"].extend(response["annotations"])

    if reasoning_buf:
        metrics["reasoning_parts"].append("".join(reasoning_buf))

    if collected_tool_calls:
        metrics["tools_call_data"].extend(collected_tool_calls)

    if not task_success:
        metrics["success"] = False
        metrics["finish_reason"] = "error"
        if error:
            metrics["last_error"] = str(error)


def finalize_main_agent_metrics(metrics):
    """Shape the aggregate into the fields needed by the history payload."""
    if not metrics:
        return None
    return {
        "input_tokens": metrics["input_tokens"],
        "output_tokens": metrics["output_tokens"],
        "total_tokens": metrics["total_tokens"],
        "expected_cost": metrics["expected_cost"],
        "latency": {
            "over_all_time": metrics["latency_total"],
            "per_task": metrics["per_task_latency"],
        },
        "tools_call_data": metrics["tools_call_data"],
        "reasoning": "\n".join(p for p in metrics["reasoning_parts"] if p),
        "service": metrics["service"],
        "model": metrics["model"],
        "finish_reason": metrics["finish_reason"],
        "success": metrics["success"],
        "firstAttemptError": metrics["firstAttemptError"],
        "AiConfig": metrics["AiConfig"],
        "fallback_model": metrics["fallback_model"] or {},
        "llm_urls": metrics["llm_urls"],
        "annotations": metrics["annotations"],
        "last_error": metrics["last_error"],
    }


def _init_main_agent_metrics():
    """
    Aggregate container for main-agent task telemetry. Primary-agent sub-tasks
    run with skip_history=True, so nothing else persists their tokens/latency/
    tools/reasoning. We sum them here and hand them to the final history update.
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "expected_cost": 0.0,
        "latency_total": 0.0,
        "per_task_latency": [],
        "tools_call_data": [],
        "_tool_calls_by_id": {},
        "reasoning_parts": [],
        "service": None,
        "model": None,
        "finish_reason": "stop",
        "success": True,
        "firstAttemptError": None,
        "AiConfig": None,
        "fallback_model": None,
        "llm_urls": [],
        "annotations": [],
        "last_error": None,
    }


def _merge_task_metrics(
    metrics,
    task_id,
    done_event,
    collected_tool_calls,
    reasoning_buf,
    task_success,
    error,
    *,
    agent_config=None,
    elapsed_seconds=None,
    fallback_model=None,
    service=None,
    model=None,
):
    """Fold a single primary-agent task's telemetry into the shared aggregate.

    `agent_config`, `fallback_model`, `service`, `model`, `elapsed_seconds`
    come from the executor context (bridge_configurations + wall-clock timing)
    because the inner chat's SSE `done` event only carries `response.data`
    and `response.usage`.  AiConfig / fallback_model / latency / firstAttemptError
    live in `result.historyParams` inside baseService and are never streamed.
    """
    if metrics is None:
        return

    usage = (done_event or {}).get("usage") or {}
    response = (done_event or {}).get("response") or {}
    data = response.get("data") or {}

    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    total_t = usage.get("total_tokens") or (input_t + output_t)
    cost = usage.get("cost") or usage.get("expected_cost") or 0

    metrics["input_tokens"] += input_t
    metrics["output_tokens"] += output_t
    metrics["total_tokens"] += total_t
    try:
        metrics["expected_cost"] += float(cost or 0)
    except (TypeError, ValueError):
        pass

    # Latency: prefer the provider-reported figure, fall back to wall-clock.
    provider_latency = data.get("latency")
    if isinstance(provider_latency, dict):
        over_all = provider_latency.get("over_all_time") or 0
    elif isinstance(provider_latency, (int, float)):
        over_all = provider_latency
    else:
        over_all = 0
    try:
        over_all = float(over_all or 0)
    except (TypeError, ValueError):
        over_all = 0.0
    if over_all <= 0 and elapsed_seconds is not None:
        try:
            over_all = float(elapsed_seconds)
        except (TypeError, ValueError):
            over_all = 0.0
    metrics["latency_total"] += over_all
    metrics["per_task_latency"].append({"task_id": task_id, "time": round(over_all, 4)})

    effective_model = data.get("model") or model
    if effective_model and not metrics["model"]:
        metrics["model"] = effective_model
    effective_service = response.get("service") or service
    if effective_service and not metrics["service"]:
        metrics["service"] = effective_service

    if response.get("firstAttemptError"):
        metrics["firstAttemptError"] = response["firstAttemptError"]

    # AiConfig = the bridge's customConfig sent to the LLM.  Pulled from the
    # bridge configuration we already have in hand.
    if agent_config and not metrics["AiConfig"]:
        metrics["AiConfig"] = agent_config

    if fallback_model and not metrics["fallback_model"]:
        metrics["fallback_model"] = fallback_model

    if response.get("llm_urls"):
        metrics["llm_urls"].extend(response["llm_urls"])
    if response.get("annotations"):
        metrics["annotations"].extend(response["annotations"])

    if reasoning_buf:
        metrics["reasoning_parts"].append("".join(reasoning_buf))

    if collected_tool_calls:
        metrics["tools_call_data"].extend(collected_tool_calls)

    if not task_success:
        metrics["success"] = False
        metrics["finish_reason"] = "error"
        if error:
            metrics["last_error"] = str(error)


def finalize_main_agent_metrics(metrics):
    """Shape the aggregate into the fields needed by the history payload."""
    if not metrics:
        return None
    return {
        "input_tokens": metrics["input_tokens"],
        "output_tokens": metrics["output_tokens"],
        "total_tokens": metrics["total_tokens"],
        "expected_cost": metrics["expected_cost"],
        "latency": {
            "over_all_time": metrics["latency_total"],
            "per_task": metrics["per_task_latency"],
        },
        "tools_call_data": metrics["tools_call_data"],
        "reasoning": "\n".join(p for p in metrics["reasoning_parts"] if p),
        "service": metrics["service"],
        "model": metrics["model"],
        "finish_reason": metrics["finish_reason"],
        "success": metrics["success"],
        "firstAttemptError": metrics["firstAttemptError"],
        "AiConfig": metrics["AiConfig"],
        "fallback_model": metrics["fallback_model"] or {},
        "llm_urls": metrics["llm_urls"],
        "annotations": metrics["annotations"],
        "last_error": metrics["last_error"],
    }


def _get_runnable_tasks(tasks):
    """Find tasks that are pending and have all dependencies completed."""
    runnable = []
    for task_id, task in tasks.items():
        if task["status"] != "pending":
            continue
        deps = task.get("dependencies", [])
        all_deps_met = all(
            tasks.get(dep, {}).get("status") == "completed" for dep in deps
        )
        if all_deps_met:
            runnable.append(task_id)
    return runnable


def _is_plan_blocked(tasks):
    """Return True when the plan cannot make any further progress on its own.

    Blocking conditions (any one triggers a pause):
      - A task is `waiting_for_user` (user must reply).
      - A task is `needs_replan` and no work is in progress (the planner's
        revision did not resolve it; the plan cannot advance without another
        planner pass or user input).
      - There are pending tasks but none have their dependencies met and
        nothing is running.
    """
    has_waiting_for_user = any(t["status"] == "waiting_for_user" for t in tasks.values())
    if has_waiting_for_user:
        return True

    has_in_progress = any(t["status"] == "in_progress" for t in tasks.values())
    has_needs_replan = any(t["status"] == "needs_replan" for t in tasks.values())
    if has_needs_replan and not has_in_progress:
        return True

    for task_id, task in tasks.items():
        if task["status"] == "pending":
            deps = task.get("dependencies", [])
            all_deps_met = all(
                tasks.get(dep, {}).get("status") == "completed" for dep in deps
            )
            if all_deps_met:
                return False  # At least one task can still run

    if has_in_progress:
        return False  # Still executing

    has_pending = any(t["status"] == "pending" for t in tasks.values())
    return has_pending


def _is_plan_complete(tasks):
    """Check if all tasks are in a terminal state."""
    return all(t["status"] in TERMINAL_STATUSES for t in tasks.values())


def _inject_variables_into_tool_args(tool_name, args, variables, variables_path, tool_id_and_name_mapping):
    """Inject static variables into tool arguments based on variables_path mapping.
    Matches the behavior of replace_variables_in_args in main flow.
    """
    if not variables_path or not variables:
        return args
    
    import pydash as _
    
    # Get the function name for variable path lookup
    tool_mapping = tool_id_and_name_mapping.get(tool_name, {})
    if tool_mapping.get("type") == "AGENT":
        function_name = tool_mapping.get("bridge_id", "")
    else:
        function_name = tool_mapping.get("name", tool_name)
    
    # Inject variables based on variables_path mapping
    enriched_args = dict(args or {})
    function_variables_path = variables_path.get(function_name, {})
    
    for path_key, path_value in function_variables_path.items():
        value_to_set = _.objects.get(variables, path_value)
        if value_to_set is not None:
            _.objects.set_(enriched_args, path_key, value_to_set)
    
    return enriched_args


async def _execute_single_task(task_id, task, org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, plan, streamer=None, main_agent_metrics=None, variables=None, variables_path=None):
    """Execute a single task by calling the appropriate agent directly.
    
    When `streamer` is provided, delta/reasoning/tool events from the agent's
    stream are forwarded to the client in real-time, tagged with `task_id`.

    When `main_agent_metrics` is provided and the task runs on the main bridge,
    per-task tokens / latency / reasoning / tool_calls are folded into the
    aggregate so the final history update can persist them.
    """
    assigned_agent = task.get("assigned_agent") or bridge_id
    is_primary_agent_task = not task.get("assigned_agent") or task.get("assigned_agent") == bridge_id
    aggregate_metrics = main_agent_metrics if is_primary_agent_task else None

    task_started_at = time.perf_counter() if aggregate_metrics is not None else None

    task_started_at = time.perf_counter() if aggregate_metrics is not None else None

    try:
        from src.services.commonServices.common import chat_multiple_agents

        current_agent_config = bridge_configurations.get(assigned_agent, {})
        if not current_agent_config:
            return {"success": False, "status": "failed", "error": f"Agent configuration not found for {assigned_agent}"}

        # Per-task prompt + tool scoping: copy only what we mutate.
        # Keep a cheap top-level map copy and deep-copy the assigned agent entry
        # so this task cannot leak prompt/tool changes to siblings.
        scoped_bridge_configurations = dict(bridge_configurations)
        scoped_agent_entry = copy.deepcopy(scoped_bridge_configurations.get(assigned_agent) or {})
        scoped_bridge_configurations[assigned_agent] = scoped_agent_entry
        scoped_agent_config = scoped_agent_entry.setdefault("configuration", {})
        filtered_tools = _filter_tools_for_task(scoped_agent_entry, task.get("assigned_tool"))
        filtered_tool_names = [
            t.get("name") or (t.get("function") or {}).get("name") or ""
            for t in filtered_tools
        ]
        filtered_tool_names = [n for n in filtered_tool_names if n]
        scoped_agent_config["tools"] = filtered_tools
        worker_prompt_template = await _get_worker_prompt_template_from_db(WORKER_SYSTEM_PROMPT_TEMPLATE)
        scoped_agent_config["prompt"] = _build_worker_system_prompt(
            task,
            filtered_tool_names,
            plan.get("tasks", {}),
            prompt_template=worker_prompt_template,
        )
        # Force JSON-object response. Live inside scoped_agent_config because
        # chat_multiple_agents does `primary_body.update(primary_config)` which
        # clobbers request_body["configuration"] with primary_config["configuration"];
        # keys placed here survive that merge. parse_request_body then reads
        # body.configuration.response_type into parsed_data.
        scoped_agent_config["response_type"] = {"type": "json_object"}
        if streamer:
            scoped_agent_config["stream"] = True

        request_body = {
            # Keep user message concise; task semantics live in system prompt.
            "user": _build_worker_user_message(task_id, task),
            "bridge_id": assigned_agent,
            "message_id": str(uuid.uuid1()),
            "thread_id": thread_id,
            "sub_thread_id": sub_thread_id,
            "org_id": org_id,
            "variables": variables or {},
            "variables_path": variables_path or {},
            "bridge_configurations": scoped_bridge_configurations,
            "plans": plan,
            # Skip per-sub-task history for the primary agent; its final
            # response is saved once by todo_handler after full execution.
            "skip_history": is_primary_agent_task,
        }

        data_to_send = {"body": request_body, "state": {}}
        response = await chat_multiple_agents(data_to_send)
        
        if hasattr(response, "body"):
            response_data = json.loads(response.body.decode("utf-8"))
            if response_data.get("success"):
                content = response_data.get("response", {}).get("data", {}).get("content", "")
                return _build_worker_result(_parse_worker_response(content))
            else:
                return {"success": False, "status": "failed", "error": response_data.get("error") or response_data.get("message") or "Task execution failed"}

        elif hasattr(response, "body_iterator"):
            accumulated_content = []
            done_event = None
            reasoning_parts = []
            tool_calls_by_id = {}
            tool_calls_order = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8")
                for line in chunk.split("\n"):
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    evt_type = event.get("event")

                    if evt_type == "delta":
                        content_piece = event.get("content", "")
                        accumulated_content.append(content_piece)
                        if streamer:
                            await streamer.emit_task_delta(task_id, content_piece)

                    elif evt_type == "reasoning":
                        reasoning_piece = event.get("content", "")
                        if aggregate_metrics is not None and reasoning_piece:
                            reasoning_parts.append(reasoning_piece)
                        if streamer:
                            await streamer.emit_task_reasoning(task_id, reasoning_piece)

                    elif evt_type == "tool_call":
                        call_id = event.get("call_id", "")
                        if aggregate_metrics is not None:
                            # Format matches main flow: {call_id: {name, args, data, id}}
                            if not call_id:
                                # Generate fallback ID if missing
                                call_id = f"tool_{task_id}_{len(tool_calls_order)}"
                            
                            if call_id not in tool_calls_by_id:
                                # Inject static variables into args before storing in history
                                # This matches main flow where replace_variables_in_args is called
                                # before process_data_and_run_tools builds tool history
                                raw_args = event.get("args", {})
                                tool_name = event.get("name", "")
                                tool_id_and_name_mapping = (bridge_configurations.get(assigned_agent, {}) or {}).get("tool_id_and_name_mapping", {})
                                enriched_args = _inject_variables_into_tool_args(
                                    tool_name, raw_args, variables, variables_path, tool_id_and_name_mapping
                                )
                                
                                # Create entry matching main flow format
                                tool_entry = {
                                    call_id: {
                                        "name": tool_name,
                                        "args": enriched_args,  # Store enriched args with injected variables
                                        "data": None,  # Will be updated when tool_result arrives
                                        "id": tool_name,  # Tool identifier
                                    }
                                }
                                tool_calls_by_id[call_id] = tool_entry[call_id]
                                tool_calls_order.append(tool_entry)
                        if streamer:
                            await streamer.emit_task_tool_call(
                                task_id,
                                name=event.get("name", ""),
                                args=event.get("args", {}),
                                call_id=call_id,
                            )

                    elif evt_type == "tool_result":
                        call_id = event.get("call_id", "")
                        result_content = event.get("content", "")
                        if aggregate_metrics is not None:
                            if call_id and call_id in tool_calls_by_id:
                                # Update the data field in existing entry
                                tool_calls_by_id[call_id]["data"] = {
                                    "response": result_content,
                                    "status": 1,
                                    "metadata": {"type": "function"}
                                }
                            else:
                                # Tool result without prior tool_call - create standalone entry
                                if not call_id:
                                    call_id = f"tool_{task_id}_{len(tool_calls_order)}"
                                tool_entry = {
                                    call_id: {
                                        "name": event.get("name", ""),
                                        "args": {},
                                        "data": {
                                            "response": result_content,
                                            "status": 1,
                                            "metadata": {"type": "function"}
                                        },
                                        "id": event.get("name", ""),
                                    }
                                }
                                tool_calls_order.append(tool_entry)
                        if streamer:
                            await streamer.emit_task_tool_result(
                                task_id,
                                name=event.get("name", ""),
                                content=result_content,
                                call_id=call_id,
                            )

                    elif evt_type == "done":
                        done_event = event

            content = "".join(accumulated_content)
            if done_event and done_event.get("response", {}).get("data", {}).get("content"):
                content = done_event["response"]["data"]["content"]

            parsed = _parse_worker_response(content)

            if aggregate_metrics is not None:
                elapsed = (
                    time.perf_counter() - task_started_at
                    if task_started_at is not None
                    else None
                )
                _merge_task_metrics(
                    aggregate_metrics,
                    task_id,
                    done_event,
                    tool_calls_order,
                    reasoning_parts,
                    task_success=parsed.get("status") != "failed",
                    error=parsed.get("error") if parsed.get("status") == "failed" else None,
                    agent_config=current_agent_config.get("configuration"),
                    elapsed_seconds=elapsed,
                    fallback_model=current_agent_config.get("fall_back"),
                    service=current_agent_config.get("service"),
                    model=(current_agent_config.get("configuration") or {}).get("model"),
                )

            return _build_worker_result(parsed)
        else:
            if response.get("success"):
                content = response.get("response", {}).get("data", {}).get("content", "")
                return _build_worker_result(_parse_worker_response(content))
            else:
                return {"success": False, "status": "failed", "error": response.get("error") or response.get("message") or "Task execution failed"}

    except Exception as e:
        logger.error(f"Error executing task {task_id}: {e}")
        return {"success": False, "status": "failed", "error": str(e)}


def _recover_interrupted_in_progress_tasks(plan):
    """Re-queue tasks that were left in_progress after an interrupted run."""
    tasks = (plan or {}).get("tasks") or {}
    recovered = []
    for task_id, task in tasks.items():
        if task.get("status") != "in_progress":
            continue
        task["status"] = "pending"
        task["is_error"] = False
        task["error"] = None
        task["result"] = None
        task["last_recovery_reason"] = INTERRUPTED_TASK_RECOVERY_NOTE
        recovered.append(task_id)
    return recovered


async def _trigger_replan(org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, replan_entry, emit):
    """Call the planner again mid-execution with the worker's replan reason and
    the current plan already in Redis. `prepare_planner_request` folds the
    existing plan into the user message automatically, so the planner emits a
    revised task set that accounts for what's already done. Completed tasks
    are preserved; everything else is replaced.
    """
    from src.services.commonServices.common import chat_multiple_agents
    from src.services.todo.planner_service import _parse_plan_json

    worker_title = replan_entry.get("title", "")
    worker_reason = replan_entry.get("reason", "")
    feedback = (
        f"Task '{worker_title}' reported the plan needs revision. "
        f"Reason: {worker_reason}. "
        "Use CURRENT PLAN statuses/results to decide the fix. "
        "If this looks like a tool/system error, first try an alternative implementation/tool path. "
        "If no reliable alternative is possible, keep the plan stable and add a clear user-facing "
        "message task (waiting_for_user) explaining there is an internal issue while performing the task. "
        "Keep completed tasks unchanged and only rewrite remaining work."
    )

    replan_body = copy.deepcopy(parsed_data)
    replan_body["user"] = feedback
    replan_body["mode"] = "plan"
    replan_body.pop("action", None)
    replan_body["message_id"] = str(uuid.uuid1())
    replan_body.setdefault("configuration", {})["stream"] = False
    replan_body["skip_history"] = True
    # chat_multiple_agents does primary_body.update(primary_config), which
    # overwrites body.configuration with bridge_configurations[bridge].configuration.
    # Deep-copy the main bridge entry and force stream=False there so the
    # planner call returns a single JSONResponse we can parse.
    scoped_bridge_configurations = copy.deepcopy(bridge_configurations)
    main_entry = scoped_bridge_configurations.get(bridge_id) or {}
    main_entry.setdefault("configuration", {})["stream"] = False
    scoped_bridge_configurations[bridge_id] = main_entry
    replan_body["bridge_configurations"] = scoped_bridge_configurations

    try:
        response = await chat_multiple_agents({"body": replan_body, "state": {}})
    except Exception as err:
        logger.error(f"Replan planner call failed: {err}")
        await emit("plan_revision_failed", {"error": str(err)})
        return

    content = ""
    if hasattr(response, "body"):
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, AttributeError) as err:
            logger.error(f"Replan response was not valid JSON: {err}")
            await emit("plan_revision_failed", {"error": "planner returned invalid JSON"})
            return
        if not payload.get("success"):
            logger.error(f"Replan planner returned error: {payload}")
            await emit("plan_revision_failed", {"error": payload.get("error") or payload.get("message") or "planner failed"})
            return
        content = payload.get("response", {}).get("data", {}).get("content", "")
    else:
        logger.error("Replan planner returned a non-JSON response object")
        await emit("plan_revision_failed", {"error": "unexpected planner response shape"})
        return

    try:
        new_plan_obj = _parse_plan_json(content)
    except ValueError as err:
        logger.error(f"Replan JSON parse failed: {err}")
        await emit("plan_revision_failed", {"error": str(err)})
        return

    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return

    new_tasks = new_plan_obj.get("tasks", {}) or {}
    existing_tasks = plan.get("tasks", {}) or {}

    # Guard: if the planner came back with an empty task set, keep the old
    # plan rather than wiping it (same safety net as _save_plan_from_result).
    if not new_tasks and existing_tasks:
        logger.warning(
            f"Replan returned 0 tasks but existing plan has {len(existing_tasks)}. "
            f"Keeping existing plan intact."
        )
        await emit("plan_revision_failed", {"error": "planner returned empty plan"})
        return

    merged_tasks = dict(new_tasks)
    # Re-inject tasks the planner dropped that must not be lost.
    # This prevents accidental truncation of remaining work (e.g., task_5..task_7
    # disappearing after a replan focused on task_3). `needs_replan` is still
    # intentionally NOT preserved — the planner owns its resolution.
    for tid, old_task in existing_tasks.items():
        if tid in merged_tasks:
            continue
        if old_task.get("status") in REPLAN_PRESERVE_STATUSES or old_task.get("human_response") is not None:
            merged_tasks[tid] = old_task
            logger.warning(
                f"Replan: planner omitted protected task '{tid}' (status={old_task.get('status')}); preserved by patch merge."
            )
    plan["tasks"] = merged_tasks
    if new_plan_obj.get("goal"):
        plan["goal"] = new_plan_obj["goal"]
    plan["state"] = "executing"
    await plan_store.update_plan(plan)

    await emit("plan_revised", {
        "triggered_by_task": replan_entry.get("task_id"),
        "replan_reason": worker_reason,
        "plan": plan,
    })


async def execute_plan(org_id, bridge_id, thread_id, sub_thread_id, bridge_configurations, parsed_data, streamer=None):
    """
    Execute all tasks respecting dependencies and parallelism.
    If `streamer` is provided, task progress events are emitted live via SSE.

    Returns the finalized main-agent metrics aggregate (tokens, latency,
    reasoning, tools_call_data, etc.) so the caller can attach it to the
    history update. Connected-agent tasks are excluded — they persist their
    own rows via the normal chat path.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        logger.error(f"Plan not found for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
        return None

    recovered_task_ids = _recover_interrupted_in_progress_tasks(plan)
    if recovered_task_ids:
        logger.warning(
            f"Recovered interrupted tasks for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}: "
            f"{recovered_task_ids}"
        )
        await plan_store.update_plan(plan)

    main_agent_metrics = _init_main_agent_metrics()

    plan["state"] = "executing"
    await plan_store.update_plan(plan)

    async def _emit(event_type, data):
        if streamer:
            await streamer.emit_delta(json.dumps({"event": event_type, **data}))

    if recovered_task_ids:
        await _emit("tasks_recovered", {
            "task_ids": recovered_task_ids,
            "reason": INTERRUPTED_TASK_RECOVERY_NOTE,
        })

    tasks = plan.get("tasks", {})

    max_iterations = 1000
    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        if not plan:
            logger.warning(f"Plan disappeared during execution at iteration {iteration}")
            return finalize_main_agent_metrics(main_agent_metrics)
        tasks = plan.get("tasks", {})

        # Check if all tasks are done
        if _is_plan_complete(tasks):
            has_failures = any(t.get("status") == "failed" for t in tasks.values())
            plan["state"] = "failed" if has_failures else "completed"
            await plan_store.update_plan(plan)
            logger.info(
                f"Plan reached terminal state={plan['state']} for "
                f"{org_id}/{bridge_id}/{thread_id}/{sub_thread_id}"
            )
            await _emit("plan_completed", {"state": plan["state"], "plan": plan})
            break

        # Check if plan is blocked
        if _is_plan_blocked(tasks):
            plan["state"] = "paused"
            await plan_store.update_plan(plan)
            logger.info(f"Plan paused (blocked) for {org_id}/{bridge_id}/{thread_id}/{sub_thread_id}")
            await _emit("plan_paused", {"state": "paused", "plan": plan})
            break

        # Find runnable tasks
        runnable = _get_runnable_tasks(tasks)
        if not runnable:
            await asyncio.sleep(1)
            continue

        # Mark runnable tasks as in_progress and notify
        for task_id in runnable:
            tasks[task_id]["status"] = "in_progress"
            tasks[task_id]["started_at"] = time.time()
            await _emit("task_started", {"task_id": task_id, "title": tasks[task_id].get("title", "")})
        await plan_store.update_plan(plan)

        # Execute runnable tasks in parallel (streamer forwarded for live events)
        # Extract variables from parsed_data for connected agent calls
        plan_variables = parsed_data.get("variables") or {}
        plan_variables_path = parsed_data.get("variables_path") or {}
        
        coroutines = [
            _execute_single_task(
                task_id, tasks[task_id],
                org_id, bridge_id, thread_id, sub_thread_id,
                bridge_configurations, plan, streamer=streamer,
                main_agent_metrics=main_agent_metrics,
                variables=plan_variables,
                variables_path=plan_variables_path,
            )
            for task_id in runnable
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # Process results
        plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
        tasks = plan.get("tasks", {})

        replan_queue = []
        for task_id, result in zip(runnable, results):
            task = tasks[task_id]

            if isinstance(result, Exception):
                result = {"success": False, "status": "failed", "error": str(result)}

            status = result.get("status") or ("completed" if result.get("success") else "failed")

            if status == "completed":
                task["status"] = "completed"
                task["is_error"] = False
                task["error"] = None
                task["result"] = result.get("result")
                await _emit("task_completed", {"task_id": task_id, "title": task.get("title", ""), "result": task["result"]})

            elif status == "waiting_for_user":
                # Worker needs user help to proceed
                human_query = result.get("human_query") or "The task needs additional information to proceed. Please provide guidance."
                task["status"] = "waiting_for_user"
                task["is_error"] = False
                task["human_query"] = human_query
                task["human_response"] = None
                await _emit("task_waiting_for_user", {
                    "task_id": task_id,
                    "title": task.get("title", ""),
                    "human_query": human_query,
                })

            else:  # failed
                task["retry"] = task.get("retry", 0) + 1
                task["is_error"] = True
                task["error"] = result.get("error")
                max_retry = task.get("max_retry", 2)
                if task["retry"] < max_retry:
                    task["status"] = "pending"
                    logger.info(f"Task {task_id} failed, retry {task['retry']}/{max_retry}")
                    await _emit("task_error", {
                        "task_id": task_id,
                        "title": task.get("title", ""),
                        "is_error": True,
                        "error": task["error"],
                        "retry": task["retry"],
                        "max_retry": max_retry,
                        "retrying": True,
                    })
                else:
                    task["status"] = "failed"
                    logger.error(f"Task {task_id} failed after {max_retry} retries: {task['error']}")
                    await _emit("task_error", {
                        "task_id": task_id,
                        "title": task.get("title", ""),
                        "is_error": True,
                        "error": task["error"] or "task execution failed",
                        "retry": task["retry"],
                        "max_retry": max_retry,
                        "retrying": False,
                    })

        await plan_store.update_plan(plan)

        # If any worker asked for a replan, call the planner now. The next loop
        # iteration re-reads the plan and runs whatever tasks the planner
        # produced in place of the pending ones.
        if replan_queue:
            for entry in replan_queue:
                await _trigger_replan(
                    org_id=org_id,
                    bridge_id=bridge_id,
                    thread_id=thread_id,
                    sub_thread_id=sub_thread_id,
                    bridge_configurations=bridge_configurations,
                    parsed_data=parsed_data,
                    replan_entry=entry,
                    emit=_emit,
                )

    # Final state check
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if plan and plan["state"] == "executing":
        if iteration >= max_iterations:
            logger.error(f"Plan execution hit max iterations ({max_iterations}) - possible infinite loop")
            plan["state"] = "failed"
            await plan_store.update_plan(plan)
            await _emit("plan_failed", {"state": "failed", "reason": "max_iterations_reached", "plan": plan})
        else:
            has_failures = any(t["status"] == "failed" for t in plan.get("tasks", {}).values())
            plan["state"] = "failed" if has_failures else "completed"
            await plan_store.update_plan(plan)
            await _emit("plan_completed", {"state": plan["state"], "plan": plan})

    return finalize_main_agent_metrics(main_agent_metrics)

    return finalize_main_agent_metrics(main_agent_metrics)


async def resume_task(org_id, bridge_id, thread_id, sub_thread_id, task_id, human_response):
    """
    Store human response and reset the task to pending.
    The caller is responsible for driving execute_plan so that execution
    events are streamed back to the client.
    """
    plan = await plan_store.get_plan(org_id, bridge_id, thread_id, sub_thread_id)
    if not plan:
        return {"success": False, "error": "Plan not found"}

    task = plan.get("tasks", {}).get(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    if task["status"] != "waiting_for_user":
        return {"success": False, "error": f"Task {task_id} is not waiting for user input (status: {task['status']})"}

    task["human_response"] = human_response
    task["status"] = "pending"
    await plan_store.update_plan(plan)

    # Save Q&A to session memory so planner doesn't ask again.
    # Scoped per (thread_id, sub_thread_id) to match the plan's scope.
    question = task.get("human_query")
    if question:
        await plan_store.add_to_planner_session(
            org_id, bridge_id, thread_id, sub_thread_id, question, human_response
        )

    return {"success": True, "plan": plan}
