"""
Reviewer agent loop.

After the main agent has fully resolved (tools, A2A, fallback retries — all done),
the framework hands the final response to a configured reviewer bridge. The reviewer
returns JSON of the form:

    {"passed": true | false, "reason": "<feedback>"}

On `passed=false`, `reason` is fed back into the main agent as a correction and the
main agent runs again. Up to MAX_REVIEW_ROUNDS attempts. Only the final main-agent
response and final reviewer response are persisted; tokens from all attempts are
summed into the saved row so cost reporting stays honest.
"""

import json
import re
import uuid
from copy import deepcopy

from globals import logger
from src.db_services.metrics_service import build_history_and_metrics_payload
from src.services.cache_service import make_json_serializable
from src.services.commonServices.queueService.queueLogService import sub_queue_obj
from src.services.utils.common_utils import (
    build_service_params,
    configure_custom_settings,
    create_latency_object,
    load_model_configuration,
    update_usage_metrics,
)
from src.services.utils.helper import Helper

MAX_REVIEW_ROUNDS = 3

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
        "Review the main agent's response above. Decide whether it correctly and "
        "completely addresses the user's query. Respond with the JSON verdict."
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


async def _publish_reviewer_history(reviewer_summary):
    """
    Publish the reviewer's conversation_log row as its own queue message.

    Independent of the main agent's process_background_tasks call so the reviewer
    save can succeed/fail on its own and shows up as a distinct payload in queue
    logs. Node-side `save_history` consumer handles a single-element list the same
    way it handles batched ones — no Node changes needed.
    """
    if not reviewer_summary or not reviewer_summary.get("history_params"):
        return
    try:
        payload = build_history_and_metrics_payload(
            reviewer_summary.get("dataset") or [],
            reviewer_summary["history_params"],
            reviewer_summary.get("version_id"),
        )
        message = make_json_serializable({"save_history": [payload]})
        await sub_queue_obj.publish_message(message)
        logger.info(
            f"Published reviewer history for bridge {reviewer_summary.get('bridge_id')} "
            f"(rounds={reviewer_summary.get('rounds')}, passed={reviewer_summary.get('passed')})"
        )
    except Exception as exc:
        logger.error(f"Failed to publish reviewer history: {exc}")


async def _call_reviewer(
    reviewer_bridge_id,
    bridge_configurations,
    review_user_message,
    parsed_data,
    thread_info,
    timer,
    streamer=None,
):
    """
    Run a single reviewer LLM call. Returns (result_dict, tokens_dict, latency_dict).

    The reviewer runs as its own bridge: its user-authored prompt, model, and tools
    are used verbatim. We override only the user message and a few execution-context
    fields. `playground=True` is set on the reviewer's params to suppress
    intermediate sendResponse notifications that would otherwise leak to the client.

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
    # Reviewer is a single-shot judge: don't carry the main agent's conversation,
    # GPT-memory, or thread history into its call.
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
        "sub_thread_id": parsed_data.get("sub_thread_id"),
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
    update_usage_metrics(reviewer_parsed_data, params, latency, result=result, success=True)

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


async def run_review_loop(
    *,
    parsed_data,
    params,
    timer,
    thread_info,
    bridge_configurations,
    main_result,
    memory,
    streamer=None,
):
    """
    Run up to MAX_REVIEW_ROUNDS rounds of review-then-revise.

    Returns (final_main_result, reviewer_summary). The reviewer_summary is None when
    no reviewer is configured, otherwise a dict with the data needed to publish the
    reviewer's own conversation_log row.

    Side effects:
    - Mutates parsed_data["usage"] so its token fields reflect the SUM of all main
      agent attempts (round 1 + retries). The caller's existing publish path then
      stores the summed totals in conversation_logs.
    - Adds review_meta into result["historyParams"]["AiConfig"] for observability.

    If `streamer` is provided (i.e. the user requested SSE streaming), reviewer
    and re-run calls stream their tokens onto the same SSE connection. Phase
    events (`review_phase`) are emitted between rounds so the client can show
    "now reviewing", "now revising", "approved/rejected", etc. emit_done is
    NOT called here — the parent finalizer owns the single emit_done.
    """
    reviewer_bridge_id = parsed_data.get("_reviewer_bridge_id") or ""
    if not reviewer_bridge_id:
        return main_result, None

    # Non-terminal agents in an A2A chain return early at common.py:365 (after the
    # transfer_agent_config branch), so any frame that reaches this loop is by
    # definition the terminal agent. We use that terminal agent's configured
    # reviewer — matching the user's "review only the final agent's output" intent.

    original_user_query = parsed_data.get("original_user") or parsed_data.get("user") or ""
    # Snapshot the conversation history that was loaded by prepare_prompt for the
    # FIRST main-agent call (any prior turns from the user's actual thread). All
    # review-round (user, assistant) pairs are appended *after* this snapshot.
    original_conversation = list(parsed_data["configuration"].get("conversation") or [])
    review_message_pairs = []  # accumulated (user, assistant) turns from review rounds
    # The user message that produced the most recent `main_response_text`. Round 1
    # used the original user query; subsequent rounds use the prior reviewer reason.
    last_main_user_turn = original_user_query

    # Snapshot main-agent round-1 tokens (already accumulated into parsed_data["usage"]
    # by the upstream update_usage_metrics call).
    summed_main_tokens = _read_usage_tokens(parsed_data["usage"])
    reviewer_tokens_accum = _zero_tokens()

    main_response_text = _extract_response_text(main_result)
    last_reviewer_result = None
    last_reviewer_parsed_data = None
    last_reviewer_latency = None
    last_reviewer_params = None
    last_verdict = {"passed": False, "reason": ""}
    rounds_run = 0

    for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
        rounds_run = round_num
        review_user_message = _build_review_user_message(
            original_user_query, main_response_text
        )

        if streamer is not None:
            try:
                await streamer.emit_review_phase("reviewer_start", round=round_num)
            except Exception as exc:
                logger.error(f"emit_review_phase(reviewer_start) failed: {exc}")

        try:
            (
                reviewer_result,
                reviewer_parsed_data,
                round_tokens,
                round_latency,
                round_params,
            ) = await _call_reviewer(
                reviewer_bridge_id,
                bridge_configurations,
                review_user_message,
                parsed_data,
                thread_info,
                timer,
                streamer=streamer,
            )
        except Exception as exc:
            logger.error(f"Reviewer call failed on round {round_num}: {exc}")
            # Abort the review loop on hard failure — keep the latest main response
            # so the user still gets an answer.
            break

        _add_tokens(reviewer_tokens_accum, round_tokens)
        last_reviewer_result = reviewer_result
        last_reviewer_parsed_data = reviewer_parsed_data
        last_reviewer_latency = round_latency
        last_reviewer_params = round_params

        verdict = parse_reviewer_json(_extract_response_text(reviewer_result))
        last_verdict = verdict

        if streamer is not None:
            try:
                await streamer.emit_review_phase(
                    "reviewer_done",
                    round=round_num,
                    passed=bool(verdict.get("passed")),
                    reason=verdict.get("reason", ""),
                )
            except Exception as exc:
                logger.error(f"emit_review_phase(reviewer_done) failed: {exc}")

        if verdict["passed"]:
            break

        if round_num == MAX_REVIEW_ROUNDS:
            # Max attempts hit — keep the most recent main response.
            break

        # Build the multi-turn conversation for the next main-agent call:
        #   developer + original_conversation
        #   + (user: round-1 user-turn) + (assistant: round-1 main response)
        #   + (user: round-2 user-turn) + (assistant: round-2 main response)
        #   + ...
        #   + (user: this round's reviewer reason)   ← carried in parsed_data["user"]
        review_message_pairs.append(
            {"role": "user", "content": last_main_user_turn or ""}
        )
        review_message_pairs.append(
            {"role": "assistant", "content": main_response_text or ""}
        )

        parsed_data["configuration"]["conversation"] = (
            original_conversation + review_message_pairs
        )
        parsed_data["user"] = verdict["reason"] or ""
        # Keep `original_user` pinned to the user's actual query so the saved
        # conversation_log row records the real question, not the reviewer feedback.
        last_main_user_turn = parsed_data["user"]

        if streamer is not None:
            try:
                await streamer.emit_review_phase("main_rerun_start", round=round_num + 1)
            except Exception as exc:
                logger.error(f"emit_review_phase(main_rerun_start) failed: {exc}")

        try:
            new_main_result, _new_main_params, _new_main_latency = await _rerun_main_agent(
                parsed_data,
                timer,
                thread_info,
                memory,
                bridge_configurations,
                streamer=streamer,
            )
        except Exception as exc:
            logger.error(f"Main-agent re-run failed on round {round_num}: {exc}")
            # Keep the previous main response; abort further review rounds.
            break

        # Update parsed_data["usage"] with the new attempt's tokens, then accumulate
        # them into our running sum (then write the running sum back so the next
        # iteration / final publish sees the cumulative total).
        latency_for_metrics = _new_main_latency
        update_usage_metrics(
            parsed_data, _new_main_params, latency_for_metrics, result=new_main_result, success=True
        )
        round_main_tokens = _read_usage_tokens(parsed_data["usage"])
        _add_tokens(summed_main_tokens, round_main_tokens)
        parsed_data["usage"]["inputTokens"] = summed_main_tokens["input"]
        parsed_data["usage"]["outputTokens"] = summed_main_tokens["output"]
        parsed_data["usage"]["total_tokens"] = summed_main_tokens["total"]
        parsed_data["usage"]["expectedCost"] = summed_main_tokens["expected_cost"]

        main_result = new_main_result
        main_response_text = _extract_response_text(main_result)

    # Stamp review_meta onto the main agent's historyParams so it shows up in the
    # main conversation_log row's AiConfig blob.
    history_params = main_result.get("historyParams") or {}
    ai_config = history_params.get("AiConfig") or {}
    if isinstance(ai_config, dict):
        ai_config = dict(ai_config)
        ai_config["review_meta"] = {
            "rounds": rounds_run,
            "passed": bool(last_verdict.get("passed")),
        }
        history_params["AiConfig"] = ai_config
    main_result["historyParams"] = history_params

    if last_reviewer_result is None or last_reviewer_parsed_data is None:
        return main_result, None

    # Pin the reviewer's history shape so process_background_tasks can build a
    # standalone conversation_log row from it.
    reviewer_history_params = last_reviewer_result.get("historyParams") or {}
    reviewer_history_params = dict(reviewer_history_params)
    reviewer_history_params["parent_id"] = parsed_data.get("bridge_id")
    reviewer_history_params["child_id"] = None
    reviewer_history_params["thread_id"] = parsed_data.get("thread_id")
    reviewer_history_params["sub_thread_id"] = parsed_data.get("sub_thread_id")
    reviewer_history_params["org_id"] = parsed_data.get("org_id")
    reviewer_history_params["user"] = original_user_query

    reviewer_ai_config = reviewer_history_params.get("AiConfig") or {}
    if isinstance(reviewer_ai_config, dict):
        reviewer_ai_config = dict(reviewer_ai_config)
        reviewer_ai_config["review_meta"] = {
            "rounds": rounds_run,
            "passed": bool(last_verdict.get("passed")),
            "target_main_message_id": parsed_data.get("message_id"),
        }
        reviewer_history_params["AiConfig"] = reviewer_ai_config

    reviewer_dataset_entry = dict(last_reviewer_parsed_data["usage"])
    # Overwrite the per-round token fields with the summed totals across all
    # reviewer rounds, so the saved row reflects total reviewer cost.
    reviewer_dataset_entry["inputTokens"] = reviewer_tokens_accum["input"]
    reviewer_dataset_entry["outputTokens"] = reviewer_tokens_accum["output"]
    reviewer_dataset_entry["total_tokens"] = reviewer_tokens_accum["total"]
    reviewer_dataset_entry["expectedCost"] = reviewer_tokens_accum["expected_cost"]

    reviewer_summary = {
        "rounds": rounds_run,
        "passed": bool(last_verdict.get("passed")),
        "final_reason": last_verdict.get("reason", ""),
        "tokens": reviewer_tokens_accum,
        "latency": last_reviewer_latency or {},
        "history_params": reviewer_history_params,
        "dataset": [reviewer_dataset_entry],
        "version_id": last_reviewer_parsed_data.get("version_id")
            or bridge_configurations.get(reviewer_bridge_id, {}).get("version_id"),
        "bridge_id": reviewer_bridge_id,
    }

    # Publish the reviewer's row as its own queue message (separate from the main
    # agent's process_background_tasks publish). Decouples the two saves so a
    # reviewer-side failure can't block the main agent's history.
    await _publish_reviewer_history(reviewer_summary)

    return main_result, reviewer_summary
