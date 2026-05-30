import copy
import json
import time
import uuid

import pydash as _

from globals import logger
from src.services.todo.executor.metrics import merge_task_metrics
from src.services.todo.executor.plan_tasks import get_tasks
from src.services.todo.executor.prompt_builder import (
    build_a2a_continuation_user_message,
    build_a2a_conversation,
    build_a2a_initial_user_message,
)
from src.services.todo.executor.request_builder import build_worker_request
from src.services.todo.executor.response_parser import build_worker_result, parse_worker_response
from src.services.todo.executor.stream_event_processor import process_worker_stream


# Services that accept OpenAI-style `response_type={"type":"json_object"}`. For
# others (notably Anthropic — serviceKeys maps `response_type` → `output_config`,
# which Anthropic rejects unless it carries the thinking-mode `effort` shape),
# we skip the flag and rely on the agent's own bridge system prompt to enforce
# the JSON envelope.
_JSON_OBJECT_RESPONSE_SERVICES = {
    "openai",
    "openai_completion",
    "groq",
    "grok",
    "mistral",
    "open_router",
}


def _service_supports_json_object_response(service) -> bool:
    return isinstance(service, str) and service.lower() in _JSON_OBJECT_RESPONSE_SERVICES


def _parse_assigned_agent(raw):
    """Normalize task['assigned_agent'] → (agent_id, agent_variables).

    New planner schema for connected-agent tasks:
        "assigned_agent": {"agent_id": "br_xyz", "agent_variables": {...}}
    Legacy/cached plans (Redis may still hold older shapes) are tolerated:
        "assigned_agent": "br_xyz"  → ("br_xyz", {})
        "assigned_agent": None      → (None, {})
    """
    if isinstance(raw, dict):
        agent_id = raw.get("agent_id")
        agent_variables = raw.get("agent_variables")
        if not isinstance(agent_variables, dict):
            agent_variables = {}
        return agent_id, agent_variables
    if isinstance(raw, str) and raw:
        return raw, {}
    return None, {}


def _apply_variables_path_to_args(
    args: dict, parent_variables: dict, child_variables_path: dict
) -> dict:
    """Port of baseService.replace_variables_in_args for one AGENT tool call.

    Mutates `args` in place exactly the way the chat-mode reference does:
    for each `path_key -> path_value` mapping, lodash-get the value from
    parent_variables and write it into `args` at path_key (dotted keys
    produce nested dicts). AI-emitted values at the same path are
    overwritten, matching the reference behavior.
    """
    if not isinstance(args, dict) or not parent_variables or not child_variables_path:
        return args
    for path_key, path_value in child_variables_path.items():
        value_to_set = _.objects.get(parent_variables, path_value)
        if value_to_set is None:
            continue
        keys = path_key.split('.')
        cursor = args
        for key in keys[:-1]:
            nxt = cursor.get(key)
            if not isinstance(nxt, dict):
                cursor[key] = {}
                nxt = cursor[key]
            cursor = nxt
        cursor[keys[-1]] = value_to_set
    return args


def _failure_result(error: str) -> dict:
    return {"success": False, "status": "failed", "error": error}


async def _call_agent(request_body: dict) -> object:
    from src.services.commonServices.common import chat_multiple_agents
    return await chat_multiple_agents({"body": request_body, "state": {}})


async def _call_chat_direct(request_body: dict) -> object:
    """Bypass chat_multiple_agents (which can redirect bridge_id via Redis cache)
    and call chat() directly with the connected agent's saved config preloaded.
    """
    from src.services.commonServices.common import chat
    return await chat({"body": request_body, "state": {}})


def _extract_content_from_response(response_data: dict) -> str:
    return response_data.get("response", {}).get("data", {}).get("content", "")


# ---------------------------------------------------------------------------
# Connected-agent execution (A2A — no worker scaffolding)
# ---------------------------------------------------------------------------

def _resolve_connected_agent(assigned_agent, bridge_configurations):
    """Return (primary_config, resolved_agent_id) for the connected agent.

    Looks up by bridge_id first; falls back to matching by `name` so legacy
    cached plans that stored the agent name still resolve."""
    primary_config = bridge_configurations.get(assigned_agent) or {}
    if primary_config:
        return primary_config, assigned_agent
    for bid, cfg in bridge_configurations.items():
        if (cfg or {}).get("name") == assigned_agent:
            return cfg, bid
    return None, assigned_agent


def _build_connected_agent_user_turn(task: dict, all_tasks: dict) -> tuple[str, list]:
    """Decide first-turn vs continuation and return (user_message, conversation).

    First turn:        user_message = execution_details (+ dep results when any),
                       conversation = []
    Continuation turn: user_message = user's answer,
                       conversation = [prior user message, prior assistant JSON]
    """
    if task.get("human_response"):
        return (
            build_a2a_continuation_user_message(task),
            build_a2a_conversation(task, all_tasks),
        )
    return build_a2a_initial_user_message(task, all_tasks), []


def _build_connected_agent_args(
    primary_config, agent_variables, parent_variables, variables_path, agent_id, user_message,
):
    """Compose the variable bag the connected agent will receive.

    Layered (later wins):
      1. primary_config.variables  — agent's static defaults
      2. agent_variables           — planner-emitted dynamic params
      3. _query                    — current user message (for {{_query}} placeholders)
      4. variables_path mapping    — gateway-injected from parent variables (authoritative)
    """
    args = copy.deepcopy(primary_config.get("variables") or {})
    if agent_variables:
        args.update(copy.deepcopy(agent_variables))
    args["_query"] = user_message
    _apply_variables_path_to_args(
        args, parent_variables or {}, (variables_path or {}).get(agent_id) or {},
    )
    return args


def _build_connected_agent_request(
    primary_config, agent_id, user_message, args,
    thread_id, sub_thread_id, org_id, bridge_configurations,
    conversation,
):
    """Build the chat() request body for the connected agent.

    Deep-copies primary_config so chat()'s in-place mutations of nested dicts
    (settings/tools/configuration) don't leak back into bridge_configurations.
    Re-asserts caller-supplied bridge_id/message_id/user since primary_config
    carries stale values from its saved bridge entry. The `user` key is
    dropped from `variables` — that name is reserved for the user-message
    field at the chat layer.

    The connected agent's bridge owns its own system prompt — including the
    JSON envelope contract (status / result / questions / error / history).
    We do NOT inject any contract from this side. We DO still set
    response_type=json_object on services that accept it, as a hard backstop."""
    request_body = copy.deepcopy(primary_config)
    request_body["bridge_id"] = agent_id
    request_body["message_id"] = str(uuid.uuid1())
    request_body["user"] = user_message
    request_body["variables"] = {k: v for k, v in args.items() if k != "user"}
    request_body["thread_id"] = thread_id
    request_body["sub_thread_id"] = sub_thread_id
    request_body["org_id"] = org_id
    request_body["bridge_configurations"] = bridge_configurations

    configuration = request_body.setdefault("configuration", {})
    configuration["stream"] = True
    if _service_supports_json_object_response(request_body.get("service")):
        configuration["response_type"] = {"type": "json_object"}
    if conversation:
        configuration["conversation"] = conversation
    return request_body


async def _read_connected_agent_response(
    response, *, task_id, streamer, variables, variables_path,
    agent_id, bridge_configurations,
):
    """Drain the chat() response and return (content, failure_or_None).

    Streams delta/tool/reasoning events through to the parent streamer when
    the response is an SSE iterator; falls back to body/dict shapes if the
    service did not stream despite stream=True."""
    if hasattr(response, "body_iterator"):
        content, *_unused = await process_worker_stream(
            response.body_iterator,
            task_id=task_id,
            aggregate_metrics=None,
            streamer=streamer,
            variables=variables,
            variables_path=variables_path,
            assigned_agent=agent_id,
            bridge_configurations=bridge_configurations,
        )
        return content, None

    if hasattr(response, "body"):
        response_data = json.loads(response.body.decode("utf-8"))
        if not response_data.get("success"):
            return "", _failure_result(
                response_data.get("error") or response_data.get("message") or "Agent call failed"
            )
        return _extract_content_from_response(response_data), None

    if isinstance(response, dict):
        if not response.get("success"):
            return "", _failure_result(
                response.get("error") or response.get("message") or "Agent call failed"
            )
        return response.get("response", {}).get("data", {}).get("content", ""), None

    return "", _failure_result("Unexpected response type from connected agent")


async def _run_connected_agent(
    task_id: str,
    task: dict,
    assigned_agent: str,
    agent_variables: dict,
    thread_id: str,
    sub_thread_id: str,
    org_id: str,
    bridge_configurations: dict,
    plan: dict,
    variables: dict,
    variables_path: dict,
    streamer,
) -> dict:
    """A2A worker-mode for connected-agent tasks.

    The connected agent owns its own system prompt (including the JSON
    envelope contract). We ship a minimal user turn:
      - first turn:        execution_details from the plan.
      - continuation turn: the user's answer; the original turn is replayed
                           via configuration.conversation so the agent has
                           the prior context as history, not as bloat in
                           the current user message.
    Streaming stays ON so reasoning/tool/delta events flow through to the
    parent streamer; we accumulate the content and parse it once at the end."""
    primary_config, assigned_agent = _resolve_connected_agent(
        assigned_agent, bridge_configurations,
    )
    if not primary_config:
        return _failure_result(
            f"Connected agent {assigned_agent} not found in bridge_configurations"
        )

    user_message, conversation = _build_connected_agent_user_turn(task, get_tasks(plan or {}))

    args = _build_connected_agent_args(
        primary_config, agent_variables, variables, variables_path,
        assigned_agent, user_message,
    )

    request_body = _build_connected_agent_request(
        primary_config, assigned_agent, user_message, args,
        thread_id, sub_thread_id, org_id, bridge_configurations,
        conversation=conversation,
    )

    # Bypass chat_multiple_agents (which would redirect bridge_id via Redis).
    response = await _call_chat_direct(request_body)
    content, failure = await _read_connected_agent_response(
        response,
        task_id=task_id,
        streamer=streamer,
        variables=variables,
        variables_path=variables_path,
        agent_id=assigned_agent,
        bridge_configurations=bridge_configurations,
    )
    if failure:
        return failure
    logger.info(
        f"[A2A {assigned_agent} task={task_id}] inner stream content "
        f"(len={len(content) if isinstance(content, str) else 'n/a'}): {str(content)[:500]!r}"
    )
    return build_worker_result(parse_worker_response(content))


# ---------------------------------------------------------------------------
# Worker execution (primary agent, JSON contract)
# ---------------------------------------------------------------------------

async def _run_worker(
    task_id: str,
    task: dict,
    request_body: dict,
    current_agent_config: dict,
    aggregate_metrics,
    task_started_at,
    streamer,
    variables: dict,
    variables_path: dict,
    bridge_configurations: dict,
) -> dict:
    """Call the primary agent in worker mode and parse its JSON response."""
    response = await _call_agent(request_body)

    assigned_agent = request_body["bridge_id"]

    if hasattr(response, "body"):
        response_data = json.loads(response.body.decode("utf-8"))
        if response_data.get("success"):
            content = _extract_content_from_response(response_data)
            return build_worker_result(parse_worker_response(content))
        return _failure_result(
            response_data.get("error") or response_data.get("message") or "Task execution failed"
        )

    if hasattr(response, "body_iterator"):
        content, done_event, reasoning_parts, tool_calls_order = await process_worker_stream(
            response.body_iterator,
            task_id=task_id,
            aggregate_metrics=aggregate_metrics,
            streamer=streamer,
            variables=variables,
            variables_path=variables_path,
            assigned_agent=assigned_agent,
            bridge_configurations=bridge_configurations,
        )
        parsed = parse_worker_response(content)

        if aggregate_metrics is not None:
            elapsed = time.perf_counter() - task_started_at if task_started_at is not None else None
            merge_task_metrics(
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

        return build_worker_result(parsed)

    if response.get("success"):
        content = response.get("response", {}).get("data", {}).get("content", "")
        return build_worker_result(parse_worker_response(content))
    return _failure_result(response.get("error") or response.get("message") or "Task execution failed")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_single_task(
    task_id: str,
    task: dict,
    org_id: str,
    bridge_id: str,
    thread_id: str,
    sub_thread_id: str,
    bridge_configurations: dict,
    plan: dict,
    streamer=None,
    main_agent_metrics: dict | None = None,
    variables: dict | None = None,
    variables_path: dict | None = None,
) -> dict:
    """Dispatch a single task to the right agent.

    - No assigned_agent / assigned_agent.agent_id == bridge_id → worker path
      (prompt + tool override, JSON response contract, metrics aggregated)
    - assigned_agent set to a connected agent → direct call path
      (agent owns its prompt/tools, plain response treated as result)

    `assigned_agent` is now `{agent_id, agent_variables}` for connected-agent
    tasks (legacy string form still accepted for backwards-compatible cached
    plans).
    """
    assigned_agent, agent_variables = _parse_assigned_agent(task.get("assigned_agent"))
    is_primary_agent_task = not assigned_agent or assigned_agent == bridge_id

    try:
        if is_primary_agent_task:
            aggregate_metrics = main_agent_metrics
            task_started_at = time.perf_counter() if aggregate_metrics is not None else None

            request_body, current_agent_config = await build_worker_request(
                task_id, task, bridge_id, thread_id, sub_thread_id, org_id,
                bridge_configurations, plan, variables or {}, variables_path or {},
                streamer=streamer,
            )
            if not current_agent_config:
                return _failure_result(f"Agent configuration not found for {bridge_id}")

            return await _run_worker(
                task_id, task, request_body, current_agent_config,
                aggregate_metrics, task_started_at, streamer,
                variables or {}, variables_path or {}, bridge_configurations,
            )

        else:
            return await _run_connected_agent(
                task_id, task, assigned_agent, agent_variables,
                thread_id, sub_thread_id, org_id,
                bridge_configurations, plan,
                variables or {}, variables_path or {},
                streamer,
            )

    except Exception as e:
        logger.error(f"Error executing task {task_id}: {e}")
        return _failure_result(str(e))
