"""
Reviewer agent loop.

After the main agent has fully resolved (tools, A2A, fallback retries — all done),
the framework hands the final response to a configured reviewer bridge. The reviewer
returns JSON of the form:

    {"passed": true | false, "reason": "<feedback>"}

On `passed=false`, `reason` is fed back into the main agent as a correction and the
main agent runs again. Up to MAX_REVIEW_ROUNDS attempts.

Persistence: every reviewer round is saved as its own conversation_log row under
the main agent's thread_id and sub_thread_id. The Node consumer reads
historyEntries[0] of each save_history queue message, so we publish one queue
message per round. The final reviewer round's message carries the full payload
(metrics_service, validateResponse, broadcast_response_webhook, save_agent_memory,
etc.) so those side-effects fire once per main turn; intermediate rounds publish
a history-only message. The main agent's row stores summed tokens across all
main-agent attempts so cost reporting stays honest.

This module is the orchestrator only. Helpers (LLM calls, history builders,
queue publish, JSON parsing, token math) live in reviewer_service_helpers.
"""

import uuid

from globals import logger
from src.db_services.metrics_service import build_history_and_metrics_payload
from src.services.commonServices.reviewer_service_helpers import (
    _add_tokens,
    _build_review_user_message,
    _build_reviewer_dataset_entry_for_round,
    _build_reviewer_history_params_for_round,
    _call_reviewer,
    _extract_response_text,
    _publish_reviewer_round,
    _read_usage_tokens,
    _rerun_main_agent,
    _zero_tokens,
    parse_reviewer_json,
)
from src.services.utils.common_utils import update_usage_metrics

MAX_REVIEW_ROUNDS = 3


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
    no reviewer is configured, otherwise a dict describing the loop outcome (used
    for logging; callers discard it today).

    Persistence: every reviewer round (1..N) is published as its own queue message
    and saved as a separate conversation_log row, all under the main agent's
    thread_id and sub_thread_id. Intermediate rounds publish a history-only message;
    the final round publishes a full payload via make_request_data_and_publish_sub_queue
    so webhook/memory/validateResponse side-effects fire ONCE per main turn (not
    once per round).

    Side effects:
    - Mutates parsed_data["usage"] so its token fields reflect the SUM of all main
      agent attempts (round 1 + retries). The caller's existing publish path then
      stores the summed totals in the main agent's conversation_log row.
    - Adds review_meta = {rounds, passed} into result["historyParams"]["AiConfig"]
      for observability on the main agent's row. Each reviewer row carries its
      own review_meta = {round_number, passed, error?} for that specific round.

    If `streamer` is provided (i.e. the user requested SSE streaming), reviewer
    and re-run calls stream their tokens onto the same SSE connection. Phase
    events (`review_phase`) are emitted between rounds so the client can show
    "now reviewing", "now revising", "approved/rejected", etc. emit_done is
    NOT called here — the parent finalizer owns the single emit_done.
    """
    reviewer_bridge_id = parsed_data.get("_reviewer_bridge_id") or ""
    if not reviewer_bridge_id:
        return main_result, None

    # Per-session reviewer sub_thread_id. All rounds (1..N) of THIS review loop
    # share it, so they group together in conversation_logs as one session.
    # Each main-agent turn invocation of run_review_loop mints a fresh one, so
    # reviewer rows from different turns don't co-mingle. thread_id stays as
    # the main agent's so reviewer rows live alongside the main thread.
    reviewer_sub_thread_id = str(uuid.uuid4())

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
    reviewer_tokens_accum = _zero_tokens()  # only used for the returned summary

    main_response_text = _extract_response_text(main_result)
    last_verdict = {"passed": False, "reason": ""}
    rounds_run = 0
    # Per-round records collected during the loop and published after it. Each
    # entry holds the full context needed to build that round's history_params,
    # dataset entry, and queue payload (intermediate vs. final).
    reviewer_rounds = []
    # Reviewer's own (user, assistant) pairs accumulated across rounds. Round N
    # sees rounds 1..N-1 — passed to _call_reviewer as prior_conversation so
    # the reviewer can refer back to its earlier verdicts (e.g. "I asked for X
    # last round, has the agent fixed it?"). Round 1 starts empty.
    reviewer_conversation_pairs = []

    for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
        rounds_run = round_num
        review_user_message = _build_review_user_message(
            original_user_query, main_response_text
        )
        round_record = {
            "round_num": round_num,
            "review_user_message": review_user_message,
            "result": None,
            "parsed_data": None,
            "params": None,
            "tokens": _zero_tokens(),
            "latency": {},
            "error": None,
            "verdict": {"passed": False, "reason": ""},
        }

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
                reviewer_sub_thread_id=reviewer_sub_thread_id,
                streamer=streamer,
                prior_conversation=reviewer_conversation_pairs,
            )
        except Exception as exc:
            logger.error(f"Reviewer call failed on round {round_num}: {exc}")
            # Capture the error on the round record so a reviewer
            # conversation_log row gets saved below with status=false and the
            # error string in `error`.
            round_record["error"] = str(exc)
            # Surface the failure to the SSE client so they don't see a silent
            # truncation (e.g. provider 429s on the reviewer call). Wrap in
            # try/except so a streaming-side failure can't shadow the original.
            if streamer is not None:
                try:
                    await streamer.emit_error(f"Reviewer call failed on round {round_num}: {exc}")
                except Exception as emit_exc:
                    logger.error(f"emit_error(reviewer_failed) failed: {emit_exc}")
            reviewer_rounds.append(round_record)
            # Abort the review loop on hard failure — keep the latest main response
            # so the user still gets an answer.
            break

        round_record["result"] = reviewer_result
        round_record["parsed_data"] = reviewer_parsed_data
        round_record["params"] = round_params
        round_record["tokens"] = round_tokens
        round_record["latency"] = round_latency
        _add_tokens(reviewer_tokens_accum, round_tokens)

        # Soft failure: execute() returned success=False (provider error,
        # apikey limit, etc). Capture the error and stop the loop so we save
        # the reviewer's failed row instead of looping into re-runs based on a
        # bogus parsed verdict.
        if not reviewer_result.get("success", True):
            round_record["error"] = (
                reviewer_result.get("error")
                or reviewer_result.get("response", {}).get("error")
                or "<reviewer call returned success=false>"
            )
            logger.error(
                f"Reviewer round {round_num} returned soft failure: {round_record['error']}"
            )
            reviewer_rounds.append(round_record)
            break

        verdict = parse_reviewer_json(_extract_response_text(reviewer_result))
        round_record["verdict"] = verdict
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
            reviewer_rounds.append(round_record)
            break

        if round_num == MAX_REVIEW_ROUNDS:
            # Max attempts hit — keep the most recent main response.
            reviewer_rounds.append(round_record)
            break

        # Append the record BEFORE attempting the main rerun so that if the
        # rerun fails, this round is still part of reviewer_rounds and gets
        # treated as the final round (carrying side-effects on publish).
        reviewer_rounds.append(round_record)

        # Accumulate this round's (review prompt, reviewer verdict) into the
        # reviewer's own conversation so the NEXT reviewer call sees its prior
        # rounds. The assistant turn carries the reviewer's full text output
        # (including the JSON verdict) — it's what the model itself produced,
        # so feeding it back as-is keeps continuity natural.
        reviewer_conversation_pairs.append(
            {"role": "user", "content": review_user_message}
        )
        reviewer_conversation_pairs.append(
            {"role": "assistant", "content": _extract_response_text(reviewer_result) or ""}
        )

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
            # Surface the failure on the SSE stream — same rationale as the
            # reviewer-failure branch above.
            if streamer is not None:
                try:
                    await streamer.emit_error(f"Main-agent re-run failed on round {round_num}: {exc}")
                except Exception as emit_exc:
                    logger.error(f"emit_error(rerun_failed) failed: {emit_exc}")
            # Keep the previous main response; abort further review rounds.
            # The current round's record was already appended above so it
            # becomes the loop's final round and publishes with side-effects.
            break

        # Update parsed_data["usage"] with the new attempt's tokens, then accumulate
        # them into our running sum (then write the running sum back so the next
        # iteration / final publish sees the cumulative total).
        # Preserve the latency already in parsed_data["usage"] — it was set
        # before the review loop with the correct over_all_time from the original
        # timer measurement. update_usage_metrics would overwrite it with a
        # zero-over_all_time value because the main timer was already consumed.
        _pre_rerun_latency = parsed_data["usage"].get("latency")
        latency_for_metrics = _new_main_latency
        update_usage_metrics(
            parsed_data, _new_main_params, latency_for_metrics, result=new_main_result, success=True
        )
        if _pre_rerun_latency is not None:
            parsed_data["usage"]["latency"] = _pre_rerun_latency
        round_main_tokens = _read_usage_tokens(parsed_data["usage"])
        _add_tokens(summed_main_tokens, round_main_tokens)
        parsed_data["usage"]["inputTokens"] = summed_main_tokens["input"]
        parsed_data["usage"]["outputTokens"] = summed_main_tokens["output"]
        parsed_data["usage"]["total_tokens"] = summed_main_tokens["total"]
        parsed_data["usage"]["expectedCost"] = summed_main_tokens["expected_cost"]

        main_result = new_main_result
        main_response_text = _extract_response_text(main_result)

    # Stamp aggregate review_meta onto the main agent's historyParams so it
    # shows up in the main conversation_log row's AiConfig blob. Per-round
    # review_meta lives on each reviewer row's AiConfig (built below).
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

    if not reviewer_rounds:
        return main_result, None

    reviewer_cfg = bridge_configurations.get(reviewer_bridge_id, {}) or {}

    # Publish each round in order. Rounds 1..N-1 use a minimal history-only
    # message (no side-effects). The final round (the last entry in
    # reviewer_rounds, regardless of why the loop terminated) publishes the
    # full make_request_data_and_publish_sub_queue payload so webhook/memory/
    # validateResponse fire ONCE per main turn.
    last_idx = len(reviewer_rounds) - 1
    for idx, record in enumerate(reviewer_rounds):
        is_final = idx == last_idx
        round_history_params = _build_reviewer_history_params_for_round(
            record, parsed_data, reviewer_cfg, reviewer_bridge_id, reviewer_sub_thread_id
        )
        round_dataset_entry = _build_reviewer_dataset_entry_for_round(
            record, parsed_data, reviewer_cfg, round_history_params
        )
        try:
            history_payload = build_history_and_metrics_payload(
                [round_dataset_entry],
                round_history_params,
                (record["parsed_data"].get("version_id") if record["parsed_data"] else None)
                or reviewer_cfg.get("version_id"),
            )
        except Exception as exc:
            logger.error(
                f"Failed to build history payload for reviewer round {record['round_num']}: {exc}"
            )
            continue

        await _publish_reviewer_round(
            history_payload=history_payload,
            reviewer_parsed_data=record["parsed_data"],
            reviewer_result=record["result"],
            reviewer_params=record["params"],
            is_final=is_final,
            bridge_id=reviewer_bridge_id,
            round_num=record["round_num"],
            error=record["error"],
        )

    last_record = reviewer_rounds[-1]
    reviewer_summary = {
        "rounds": rounds_run,
        "passed": bool(last_verdict.get("passed")),
        "final_reason": last_verdict.get("reason", ""),
        "error": last_record["error"],
        "tokens": reviewer_tokens_accum,
        "latency": last_record["latency"] or {},
        "bridge_id": reviewer_bridge_id,
    }

    return main_result, reviewer_summary
