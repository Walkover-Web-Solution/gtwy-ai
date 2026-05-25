"""
Internal helpers for the reviewer agent loop.

Split out of reviewer_service.py to keep the orchestrator (`run_review_loop`)
readable. Nothing here is part of the public surface — `run_review_loop` is the
only function downstream code is expected to call. These helpers cover:

- JSON parsing and message templating for the reviewer's verdict
- Token-dict math for accumulating usage across rounds
- Per-round LLM calls (`_call_reviewer`, `_rerun_main_agent`)
- Per-round persistence (`_build_reviewer_history_params_for_round`,
  `_build_reviewer_dataset_entry_for_round`, `_publish_reviewer_round`)
"""

import json
import re
import time as _time_module
import uuid
from copy import deepcopy

from globals import logger
from src.services.cache_service import make_json_serializable
from src.services.commonServices.baseService.utils import make_request_data_and_publish_sub_queue
from src.services.commonServices.queueService.queueLogService import sub_queue_obj
from src.services.commonServices.queueService.queueMetricsService import metrics_queue_obj
from src.services.utils.common_utils import (
    _attach_sub_thread_extras,
    build_service_params,
    configure_custom_settings,
    create_latency_object,
    load_model_configuration,
    update_usage_metrics,
)
from src.services.utils.helper import Helper

# Appended to the reviewer's user-authored system prompt at runtime so the model
# returns a strictly-parseable verdict. We don't mutate the stored bridge prompt —
# this is added to a deep-copy of the reviewer's configuration on every call.
REVIEWER_JSON_TEMPLATE = (
    "\n\n---\n"
    "OUTPUT FORMAT (REQUIRED):\n"
    "After your reasoning, your final output MUST be a single JSON object on its own "
    "line, with no surrounding prose, no code fences, and no commentary after it. "
    'The JSON must be exactly: {"passed": <true|false>, "reason": "<string>"}\n'
    "- passed=true  → the main agent's response satisfies the user's query fully and "
    "correctly. In this case `reason` MUST be an empty string.\n"
    "- passed=false → the response is wrong, incomplete, off-topic, or low-quality. "
    "In this case `reason` MUST be specific, actionable feedback that the main agent "
    "can act on to fix its response on the next attempt.\n"
    "Output exactly one JSON object. Nothing after it."
)


def parse_reviewer_json(raw_text):
    """
    Extract {"passed": bool, "reason": str} from the reviewer's text output.

    Forgiving on purpose: a malformed reviewer reply must never crash the request.
    On parse failure, treat as passed=false with an explanatory reason so the main
    agent gets one more chance.
    """
    if not raw_text:
        return {"passed": False, "reason": "<reviewer returned empty response>"}

    text = str(raw_text).strip()

    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates.extend(fenced)

    # Depth-aware scan: find every balanced top-level {...} block (handles nested
    # braces in `reason` strings that a flat regex would skip). Quotes and escapes
    # are honored so braces inside string literals don't shift depth.
    depth = 0
    start = -1
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start : idx + 1])
                start = -1

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict) or "passed" not in parsed:
            continue
        passed = parsed.get("passed")
        reason = parsed.get("reason", "")
        if isinstance(passed, str):
            passed = passed.strip().lower() in ("true", "yes", "1", "pass", "passed")
        return {"passed": bool(passed), "reason": str(reason or "")}

    return {"passed": False, "reason": "<reviewer output was unparseable>"}


def _build_review_user_message(original_user_query, main_response_text):
    return (
        "User's original query:\n"
        f"{original_user_query}\n\n"
        "Main agent's response:\n"
        f"{main_response_text}\n\n"
    )


def _extract_response_text(result):
    if not isinstance(result, dict):
        return ""
    response_data = (result.get("response") or {}).get("data") or {}
    content = response_data.get("content")
    if isinstance(content, list):
        # Some providers return content as list of parts
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


def _zero_tokens():
    return {"input": 0, "output": 0, "total": 0, "expected_cost": 0.0}


def _read_usage_tokens(usage):
    return {
        "input": usage.get("inputTokens", 0) or 0,
        "output": usage.get("outputTokens", 0) or 0,
        "total": usage.get("total_tokens", 0) or 0,
        "expected_cost": usage.get("expectedCost", 0) or 0.0,
    }


def _add_tokens(accum, delta):
    accum["input"] += delta["input"] or 0
    accum["output"] += delta["output"] or 0
    accum["total"] += delta["total"] or 0
    accum["expected_cost"] += delta["expected_cost"] or 0.0


async def _publish_reviewer_round(
    *,
    history_payload,
    reviewer_parsed_data,
    reviewer_result,
    reviewer_params,
    is_final,
    bridge_id,
    round_num,
    error,
):
    """
    Publish one reviewer round's history to the queue.

    The Node consumer (`saveConversationHistory`) reads only `historyEntries[0]`
    of each `save_history` message, so we publish one queue message per round.

    - **Final round** (loop is terminating on this round) with full per-round
      data available: send the full payload via make_request_data_and_publish_sub_queue
      so metrics_service, validateResponse, broadcast_response_webhook,
      save_agent_memory, and the other side-effect handlers fire ONCE per main
      turn. Falls back to history-only when reviewer_parsed_data/result/params
      are None (hard-exception case).
    - **Intermediate round**: send a minimal `{"save_history": [history_payload]}`
      message — no side-effects fire here, so webhooks/memory aren't triggered
      multiple times per main turn.
    """
    try:
        conversation_log_data = history_payload["conversation_log_data"]
        metrics_data = history_payload.get("metrics_data", [])

        # Attach thread_flag and response_format to the conversation_log row.
        if reviewer_parsed_data is not None:
            _attach_sub_thread_extras(conversation_log_data, reviewer_parsed_data)
        else:
            # Hard-exception rounds have no reviewer parsed_data — synthesize the
            # two extras so the saved row is still well-formed on the Node side.
            conversation_log_data["thread_flag"] = None
            conversation_log_data["response_format"] = {"type": "default"}

        send_full_payload = (
            is_final
            and reviewer_parsed_data is not None
            and reviewer_result is not None
            and reviewer_params is not None
        )

        if send_full_payload:
            data = await make_request_data_and_publish_sub_queue(
                reviewer_parsed_data, reviewer_result, reviewer_params, thread_info=None
            )
            data["save_history"] = [conversation_log_data]
            message = make_json_serializable(data)
            kind = "final, full payload"
        else:
            message = make_json_serializable({"save_history": [conversation_log_data]})
            kind = "final, history-only" if is_final else "intermediate"

        await sub_queue_obj.publish_message(message)
        if metrics_data:
            await metrics_queue_obj.publish_message(
                make_json_serializable({"save_metrics": metrics_data})
            )
        logger.info(
            f"Published reviewer round {round_num} ({kind}) for bridge {bridge_id} "
            f"(error={error!r})"
        )
    except Exception as exc:
        logger.error(f"Failed to publish reviewer round {round_num}: {exc}")


async def _call_reviewer(
    reviewer_bridge_id,
    bridge_configurations,
    review_user_message,
    parsed_data,
    thread_info,
    timer,
    reviewer_sub_thread_id,
    streamer=None,
    prior_conversation=None,
):
    """
    Run a single reviewer LLM call. Returns (result_dict, tokens_dict, latency_dict).

    The reviewer runs as its own bridge: its user-authored prompt, model, and tools
    are used verbatim. We override only the user message and a few execution-context
    fields. `playground=True` is set on the reviewer's params to suppress
    intermediate sendResponse notifications that would otherwise leak to the client.

    `reviewer_sub_thread_id` is the per-session id minted in run_review_loop —
    shared across all rounds of THIS review session, distinct from the main
    agent's sub_thread_id. The reviewer row's thread_id stays as the main
    agent's.

    `prior_conversation` carries the reviewer's own (user, assistant) pairs from
    rounds 1..N-1 of the current review loop. When non-empty, it is set as
    `configuration["conversation"]` so each per-service conversation builder
    (createOpenAiConversation, createAnthropicConversation, etc.) renders the
    multi-turn history for the reviewer model. The main agent's conversation,
    GPT-memory, and bridge-stored conversation are still stripped — the reviewer
    only sees its own prior rounds.

    If `streamer` is provided, the reviewer streams its tokens onto the SAME SSE
    connection as the main agent — the user sees the verdict being produced in
    real time. We DO NOT call emit_done on the streamer here; the parent finalizer
    owns the single emit_done at the end of the whole review flow.
    """
    reviewer_cfg = bridge_configurations.get(reviewer_bridge_id) or {}
    if not reviewer_cfg:
        raise ValueError(f"Reviewer bridge {reviewer_bridge_id} config not loaded")

    reviewer_configuration = deepcopy(reviewer_cfg.get("configuration") or {})
    reviewer_service = reviewer_cfg.get("service") or ""
    reviewer_apikey = reviewer_cfg.get("apikey")
    reviewer_model = reviewer_configuration.get("model") or ""

    # Append the JSON-output template to the reviewer's prompt at runtime. The user
    # who authored the reviewer bridge doesn't need to know about the JSON contract;
    # we enforce it here so parse_reviewer_json can reliably extract the verdict.
    base_prompt = reviewer_configuration.get("prompt") or ""
    reviewer_configuration["prompt"] = base_prompt + REVIEWER_JSON_TEMPLATE

    # Streaming: if a parent streamer is supplied, let the reviewer stream its
    # tokens onto the same SSE connection. Otherwise force non-streaming so we
    # can buffer the full response to parse the JSON verdict.
    use_streaming = streamer is not None
    reviewer_configuration["stream"] = use_streaming
    # Replace the reviewer bridge's stored conversation with this loop's
    # accumulated reviewer pairs (round 1..N-1). On round 1, prior_conversation
    # is empty/None so the reviewer is single-shot — same as before.
    if prior_conversation:
        reviewer_configuration["conversation"] = list(prior_conversation)
    else:
        reviewer_configuration.pop("conversation", None)

    model_config, custom_config, model_output_config = await load_model_configuration(
        reviewer_model, reviewer_configuration, reviewer_service
    )
    custom_config = await configure_custom_settings(
        model_config["configuration"], custom_config, reviewer_service
    )
    custom_config["stream"] = use_streaming

    reviewer_parsed_data = {
        "configuration": reviewer_configuration,
        "apikey": reviewer_apikey,
        "variables": reviewer_cfg.get("variables") or {},
        "user": review_user_message,
        "original_user": review_user_message,
        "org_id": parsed_data.get("org_id"),
        "bridge_id": reviewer_bridge_id,
        "bridge": reviewer_cfg.get("bridge"),
        "thread_id": parsed_data.get("thread_id"),
        "sub_thread_id": reviewer_sub_thread_id,
        "model": reviewer_model,
        "service": reviewer_service,
        "is_playground": True,  # suppress intermediate sendResponse leaks to client
        "template": reviewer_cfg.get("template"),
        "response_format": {"type": "default"},
        "execution_time_logs": [],
        "variables_path": reviewer_cfg.get("variables_path") or {},
        "message_id": str(uuid.uuid4()),
        "bridgeType": reviewer_cfg.get("bridgeType"),
        "tool_id_and_name_mapping": reviewer_cfg.get("tool_id_and_name_mapping") or {},
        "reasoning_model": reviewer_cfg.get("reasoning_model", False),
        "apikey_object_id": reviewer_cfg.get("apikey_object_id"),
        "images": [],
        "audios": [],
        "maximum_iterations": reviewer_cfg.get("maximum_iterations") or 10,
        "rag_data": reviewer_cfg.get("rag_data"),
        "name": reviewer_cfg.get("name") or "",
        "org_name": reviewer_cfg.get("org_name") or "",
        "built_in_tools": reviewer_cfg.get("built_in_tools") or [],
        "files": [],
        "file_data": None,
        "youtube_url": None,
        "web_search_filters": reviewer_cfg.get("web_search_filters") or {},
        "folder_id": reviewer_cfg.get("folder_id"),
        "owner_id": reviewer_cfg.get("owner_id"),
        "limit": reviewer_cfg.get("limit"),
        "is_embed": reviewer_cfg.get("is_embed", False),
        "user_id": reviewer_cfg.get("user_id"),
        "api_collection": reviewer_cfg.get("api_collection") or {},
        "tools_call_data": [],
        "tokens": {},
        "usage": {},
    }

    params = build_service_params(
        reviewer_parsed_data,
        custom_config,
        model_output_config,
        thread_info=None,
        timer=timer,
        memory=None,
        bridge_configurations=bridge_configurations,
    )
    class_obj = await Helper.create_service_handler(params, reviewer_service)
    # When the parent provides a streamer, override the reviewer's auto-created
    # streamer so deltas flow to the same SSE connection. Mirrors the A2A
    # pattern at common.py:292 (_injected_streamer).
    if use_streaming:
        class_obj.streamer = streamer
        class_obj.stream_mode = True
    _reviewer_call_start = _time_module.time()
    result = await class_obj.execute()

    if not isinstance(result, dict):
        result = {"success": False, "response": {"data": {"content": ""}, "usage": {}}, "historyParams": {}}
    if not result.get("response"):
        result["response"] = {"data": {"content": ""}, "usage": {}}

    # The reviewer runs with playground=True (to suppress sendResponse leaks to the
    # client), but that branch in execute() skips historyParams construction. Build
    # it explicitly here so the reviewer's conversation_log row gets a proper
    # AiConfig, model, prompt, etc.
    if not result.get("historyParams"):
        try:
            result["historyParams"] = class_obj.prepare_history_params(
                result.get("response") or {},
                result.get("modelResponse") or {},
                {},
            )
        except Exception as exc:
            logger.error(f"prepare_history_params failed for reviewer: {exc}")
            result["historyParams"] = {}

    if reviewer_parsed_data.get("type") != "image":
        reviewer_parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
            reviewer_model, reviewer_service
        )
        result.setdefault("response", {}).setdefault("usage", {})
        result["response"]["usage"]["cost"] = reviewer_parsed_data["tokens"].get("total_cost") or 0

    latency = create_latency_object(timer, params)
    # The main timer is already consumed before the review loop starts, so
    # over_all_time comes out as 0. Override it with the actual wall-clock
    # time for this reviewer call instead.
    latency["over_all_time"] = round(_time_module.time() - _reviewer_call_start, 4)
    # Honor the actual success flag from execute() — passing success=True
    # unconditionally would mask soft failures and skip the error field on the
    # dataset row, leaving us with a saved reviewer log that has no error.
    reviewer_success = bool(result.get("success", True))
    reviewer_error = result.get("error") if not reviewer_success else None
    update_usage_metrics(
        reviewer_parsed_data,
        params,
        latency,
        result=result if reviewer_success else None,
        error=reviewer_error,
        success=reviewer_success,
    )

    tokens = _read_usage_tokens(reviewer_parsed_data["usage"])
    return result, reviewer_parsed_data, tokens, latency, params


async def _rerun_main_agent(
    parsed_data,
    timer,
    thread_info,
    memory,
    bridge_configurations,
    streamer=None,
):
    """
    Re-run the main agent. The caller is responsible for setting up the message
    structure: parsed_data["user"] (the latest user turn — typically the reviewer's
    feedback) and parsed_data["configuration"]["conversation"] (the prior turns,
    including each prior round's user/assistant pair).

    Disables fallback and A2A transfers for re-runs to keep the blast radius
    bounded and token accounting honest.

    If `streamer` is provided, the re-run streams its tokens onto the same SSE
    connection as the original main-agent stream — emit_done is owned by the
    parent finalizer and is not called here.
    """
    use_streaming = streamer is not None
    parsed_data["configuration"]["stream"] = use_streaming

    model_config, custom_config, model_output_config = await load_model_configuration(
        parsed_data["model"], parsed_data["configuration"], parsed_data["service"]
    )
    custom_config = await configure_custom_settings(
        model_config["configuration"], custom_config, parsed_data["service"]
    )
    custom_config["stream"] = use_streaming

    params = build_service_params(
        parsed_data,
        custom_config,
        model_output_config,
        thread_info,
        timer,
        memory,
        bridge_configurations,
    )

    class_obj = await Helper.create_service_handler(params, parsed_data["service"])
    if use_streaming:
        class_obj.streamer = streamer
        class_obj.stream_mode = True
    result = await class_obj.execute()

    if not isinstance(result, dict):
        result = {"success": False, "response": {"data": {"content": ""}, "usage": {}}, "historyParams": {}}
    if not result.get("response"):
        result["response"] = {"data": {"content": ""}, "usage": {}}

    # Strip any transfer_agent_config so the caller doesn't think a re-run is also
    # an A2A transfer (we explicitly disable A2A on re-runs).
    result.pop("transfer_agent_config", None)

    if parsed_data.get("type") != "image":
        parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
            parsed_data["model"], parsed_data["service"]
        )
        result["response"].setdefault("usage", {})
        result["response"]["usage"]["cost"] = parsed_data["tokens"].get("total_cost") or 0

    latency = create_latency_object(timer, params)
    return result, params, latency


def _build_reviewer_history_params_for_round(
    record,
    parsed_data,
    reviewer_cfg,
    reviewer_bridge_id,
    reviewer_sub_thread_id,
):
    """
    Build per-round history_params used by build_history_and_metrics_payload.

    For successful rounds, starts from the reviewer's own historyParams (which
    prepare_history_params populated with model/AiConfig/prompt/etc). For
    hard-exception rounds (no reviewer call landed), synthesizes a minimal
    one from the reviewer's bridge config so the saved row still has the
    fields the conversation_log column map expects.

    Reviewer rows live under the main agent's thread_id and a per-session
    reviewer_sub_thread_id (minted once in run_review_loop and shared across
    all rounds of THIS review session). Subsequent main-agent turns get a
    fresh reviewer_sub_thread_id, isolating reviewer rows across turns.
    """
    reviewer_result = record["result"]
    reviewer_parsed_data = record["parsed_data"]
    reviewer_error = record["error"]
    verdict = record["verdict"]
    round_num = record["round_num"]
    review_user_message = record["review_user_message"]

    if reviewer_result is not None:
        history_params = dict(reviewer_result.get("historyParams") or {})
    else:
        # Hard-exception path: no reviewer call returned. Synthesize from cfg.
        reviewer_cfg_configuration = reviewer_cfg.get("configuration") or {}
        history_params = {
            "thread_id": parsed_data.get("thread_id"),
            "sub_thread_id": reviewer_sub_thread_id,
            "user": review_user_message,
            "message": "",
            "model": reviewer_cfg_configuration.get("model", ""),
            "service": reviewer_cfg.get("service", ""),
            "bridge_id": reviewer_bridge_id,
            "AiConfig": reviewer_cfg_configuration,
            "prompt": reviewer_cfg_configuration.get("prompt", ""),
            "channel": "chat",
            "type": "assistant",
            "actor": "user",
            "tools": {},
            "chatbot_message": "",
            "tools_call_data": [],
            "message_id": str(uuid.uuid4()),
        }

    history_params["parent_id"] = parsed_data.get("bridge_id")
    history_params["child_id"] = None
    history_params["thread_id"] = parsed_data.get("thread_id")
    history_params["sub_thread_id"] = reviewer_sub_thread_id
    history_params["org_id"] = parsed_data.get("org_id")
    # Pin `user` to the templated review prompt for THIS round. On synthesized
    # hard-error rows prepare_history_params didn't run, so this fills the gap;
    # on successful rounds it's already correct but pinning keeps both
    # branches consistent.
    history_params["user"] = review_user_message
    if reviewer_error:
        # build_history_and_metrics_payload reads history_params.error only
        # when dataset[0].success is True; mirror it here so the field is
        # present regardless of which branch the consumer hits.
        history_params["error"] = reviewer_error

    ai_config = history_params.get("AiConfig") or {}
    if isinstance(ai_config, dict):
        ai_config = dict(ai_config)
        review_meta = {
            "round_number": round_num,
            "passed": bool(verdict.get("passed")),
            "target_main_message_id": parsed_data.get("message_id"),
        }
        if reviewer_error:
            review_meta["error"] = reviewer_error
        ai_config["review_meta"] = review_meta
        history_params["AiConfig"] = ai_config

    return history_params


def _build_reviewer_dataset_entry_for_round(
    record,
    parsed_data,
    reviewer_cfg,
    history_params,
):
    """
    Build the per-round dataset entry. Tokens are this round's actual tokens
    (not summed across rounds) so each saved row reflects its own cost.
    """
    reviewer_parsed_data = record["parsed_data"]
    reviewer_error = record["error"]

    if reviewer_parsed_data is not None:
        entry = dict(reviewer_parsed_data.get("usage") or {})
    else:
        entry = {
            "orgId": parsed_data.get("org_id"),
            "service": history_params.get("service", ""),
            "model": history_params.get("model", ""),
            "apikey_object_id": reviewer_cfg.get("apikey_object_id"),
            "variables": {},
            "latency": "{}",
        }

    entry["inputTokens"] = record["tokens"]["input"]
    entry["outputTokens"] = record["tokens"]["output"]
    entry["total_tokens"] = record["tokens"]["total"]
    entry["expectedCost"] = record["tokens"]["expected_cost"]

    if reviewer_error:
        # Critical: dataset[0].success=False + dataset[0].error=<msg> is what
        # build_history_and_metrics_payload uses to populate the row's `error`
        # column and `status` flag. Without this the saved row would mask the
        # failure as a successful reviewer pass.
        entry["success"] = False
        entry["error"] = reviewer_error

    return entry
