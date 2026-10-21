---
covers: ["src/services/commonServices/reviewer_service.py", "src/services/commonServices/reviewer_service_helpers.py", "src/services/utils/guardrails_validator.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Reviewer agent loop and guardrails

The reviewer loop checks the main agent's final answer and asks it to revise. A reviewer (another agent, an inline prompt, or a webhook tool) returns `{"passed", "reason"}`. On a fail, the main agent runs again with `reason` as its next user turn. There are at most `MAX_REVIEW_ROUNDS` (3) reviews per turn. `chat()` and the streaming finalizer call it after the main response succeeds and before post-tool runs. The guardrails validator in this area is an input-moderation check, but **nothing calls it**; see [Guardrails](#guardrails-present-but-not-wired-in).

| File | Role |
|---|---|
| `src/services/commonServices/reviewer_service.py` | `run_review_loop`, the only public entry point. Runs the review/re-run rounds, sums main-agent tokens, stamps `review_meta`, and publishes one history row per reviewer round. |
| `src/services/commonServices/reviewer_service_helpers.py` | Private helpers: `REVIEWER_JSON_TEMPLATE`, `parse_reviewer_json`, `_call_reviewer` (LLM reviewer), `_call_tool_reviewer` (webhook reviewer), `_rerun_main_agent`, the per-round history and dataset builders, `_publish_reviewer_round`, and token-dict math. |
| `src/services/utils/guardrails_validator.py` | `GUARDRAILS_TEMPLATES` (10 categories), `validate_with_guardrails` (one gpt-5-nano call) and `guardrails_check`. Nothing calls them. |

Callers: `src/services/commonServices/common.py:chat` (non-streaming) and `src/services/utils/common_utils.py:sse_stream_and_finalize` (streaming). See [chat-orchestration](chat-orchestration.md) and [the chat flow](../flows/chat-completion.md).

## How it works

### Turning the reviewer on

The reviewer is configured per agent under `settings.review_agent` in the agent document:

```json
"settings": { "review_agent": {
  "reviewer_enabled": true,              // optional; defaults to "any of the next three is set"
  "reviewer_agent":  "<agent _id>",      // a separate agent that judges
  "reviewer_prompt": "<system prompt>",  // inline reviewer prompt, run on the MAIN agent's model
  "reviewer_tools":  ["<apicalls _id>"]  // webhook reviewer; only the first id is used
} }
```

Loading happens in [agent-configuration](agent-configuration.md):
- `src/services/utils/getConfiguration.py:_collect_connected_agent_configs` loads `reviewer_agent` as one more entry in `bridge_configurations`. If that load fails, it is logged and skipped.
- `src/services/utils/getConfiguration.py:getConfiguration` turns `reviewer_tools_data` into `cfg["reviewer_tools"] = [{"script_id", "url", "title", "_id"}]`. But since #1752 `src/db_services/ConfigurationServices.py:get_bridges_with_tools_and_apikeys` no longer looks reviewer tools up in `apicalls`, so `reviewer_tools_data` is always empty and `cfg["reviewer_tools"]` is always `[]`. A webhook-only reviewer therefore fails on round 1 (see the TODO in [agent-configuration](agent-configuration.md)).

`src/services/commonServices/common.py:chat` reads the current frame's own agent settings, `bridge_configurations[parsed_data["bridge_id"]]["settings"]["review_agent"]`, and sets private keys on `parsed_data`:

| Key | Set when | Value |
|---|---|---|
| `_reviewer_bridge_id` | `reviewer_agent` is set **and** loaded into `bridge_configurations` | agent id |
| `_reviewer_prompt` | first non-empty of `body.reviewer_prompt`, `body.settings.review_agent.reviewer_prompt`, the agent setting | string |
| `_reviewer_tools` | `reviewer_tools` is non-empty | the raw id list from settings (not the resolved dicts) |

If `reviewer_enabled` is false, none of these keys are set. If none of the three are set, the loop is skipped. For normal agent calls, the middleware merge overwrites the request's `reviewer_prompt` and `settings`, so in practice the agent's stored values win ([agent-configuration](agent-configuration.md)).

Every `chat()` frame that reaches the success tail runs its own agent's reviewer. That includes agent-as-tool children, because they re-enter `chat()` ([tool-calling](tool-calling.md)). In a transfer chain, the non-terminal agents return before the loop, so only the last agent's reviewer runs.

### Reviewer modes

`run_review_loop` and `_call_reviewer` choose the mode in this order:

| Mode | Condition | What runs |
|---|---|---|
| Prompt | `_reviewer_prompt` set (wins even when `_reviewer_bridge_id` is also set) | The **main** agent's model, service and apikey, using `reviewer_prompt` as the system prompt. Its `tools`, `response_type` and `response_format` are removed. |
| Agent | `_reviewer_bridge_id` set, no prompt | The reviewer agent's own config: prompt, model, service, apikey, `built_in_tools`, `api_collection`. |
| Webhook | only `_reviewer_tools` set | `_call_tool_reviewer` POSTs `{user, ai_response, bridge_id, thread_id, org_id}` to the first tool's `url` through `src/services/commonServices/baseService/utils.py:axios_work`. It costs nothing and counts no tokens. A tool with no `url` raises `ValueError`, which ends the loop as a hard failure on round 1. |

In the two LLM modes, `REVIEWER_JSON_TEMPLATE` is appended to a deep copy of the prompt, so agent authors never write the JSON contract themselves. If the main agent's cfg has `reviewer_tools`, they replace `configuration["tools"]` in the LLM reviewer too (see the TODO under Gotchas).

### The loop (`run_review_loop`)

1. Before the loop, the caller has finished round 1. That includes cost (`parsed_data["tokens"]`), `update_usage_metrics`, and, in `chat()`, fallback, transfer, JSON repair and template rendering.
2. The loop snapshots `original_user_query` (`original_user`, falling back to `user`), the loaded `configuration.conversation`, and the round-1 tokens from `parsed_data["usage"]`. It mints one `reviewer_sub_thread_id` (uuid4) for this turn.
3. Each round builds the review message from the original query and the **latest** main response text, then calls the reviewer. The LLM reviewer sees only its own earlier rounds as conversation (`prior_conversation`). It never sees the main agent's history, memory or tool results.
4. The loop stops when any of these happens:
   - the verdict passes;
   - round 3 is reached (the latest main answer is kept);
   - the reviewer raises (hard failure; the round is recorded with `error`);
   - the reviewer returns `success: False` (soft failure);
   - the main re-run raises.

   In every case the user gets the latest main answer.
5. On a fail with rounds left:
   - The (review message, reviewer output) pair is added to the reviewer's own conversation.
   - The (last main user turn, main response) pair is added to the main conversation: `configuration.conversation = original + pairs`.
   - `parsed_data["user"]` is set to `reason`.
   - `_rerun_main_agent` runs the main agent again.
6. `_rerun_main_agent` builds fresh params and calls `execute()` directly. That means **no fallback**. Any `transfer_agent_config` is popped, so **no A2A transfer** happens either. It also skips JSON repair, template rendering and response caching. It reuses `parsed_data["message_id"]`, so it shares round 1's tool budget key in `src/services/utils/maximum_iterations_utils.py:build_tool_count_key`.
7. After each re-run, `update_usage_metrics` runs. Then `inputTokens`, `outputTokens`, `total_tokens` and `expectedCost` in `parsed_data["usage"]` are overwritten with the running sum over all main attempts. The pre-loop `latency` is put back, because the main timer is already used up. `tools_call_data` from every attempt is accumulated.
8. After the loop, `historyParams.AiConfig.review_meta = {rounds, passed}` is stamped on the main result, and the accumulated `tools_call_data` replaces the result's list.
9. Each recorded round is published in order (see Contracts). The function returns `(main_result, reviewer_summary)`. Both callers throw `reviewer_summary` away.

Worst case per turn: 3 reviewer calls and 2 main re-runs, on top of round 1.

### Streaming

When `sse_stream_and_finalize` passes `streamer`, the reviewer and the re-runs stream their tokens onto the same SSE connection: `class_obj.streamer` is swapped and `stream_mode` is set. The user sees the reviewer's own text, including the JSON verdict, as deltas. Phase markers go out through `src/services/commonServices/streaming_service.py:emit_review_phase`:

```json
{"event": "review_phase", "phase": "reviewer_start",   "round": 1}
{"event": "review_phase", "phase": "reviewer_done",    "round": 1, "passed": false, "reason": "..."}
{"event": "review_phase", "phase": "main_rerun_start", "round": 2}
```

Failures send `emit_error`. The loop never calls `emit_done`; the parent finalizer sends the single `done`.

## Contracts

```python
async def run_review_loop(*, parsed_data, params, timer, thread_info,
                          bridge_configurations, main_result, memory, streamer=None)
    -> tuple[dict, dict | None]   # (final main result, summary or None)
```

Verdict, from `parse_reviewer_json` or `_call_tool_reviewer`:

```json
{"passed": true, "reason": ""}            // passed=true → reason must be ""
{"passed": false, "reason": "<actionable feedback for the main agent>"}
```

- `parse_reviewer_json` tries three things in order: the whole text, fenced `json` blocks, then every balanced top-level `{...}` (it honours quotes and escapes). It takes the first dict that has `passed`. String values such as `"true"`, `"yes"`, `"1"` and `"pass"` count as passed. Empty output becomes `passed=false` with reason `<reviewer returned empty response>`. Output it cannot parse becomes `passed=false` with reason `<reviewer output was unparseable>`.
- The webhook reviewer reads `passed`/`reason` from the top level of the response or from `response.response`. HTTP status 0 means a failed call (`success: False`).

The review message sent to the reviewer as its user turn (`_build_review_user_message`):

```text
User's original query:
<original query>

Main agent's response:
<latest main response text>
```

The reviewer's `parsed_data` (built in `_call_reviewer`; key fields only):

```json
{"bridge_id": "<effective reviewer id>", "thread_id": "<main thread_id>", "sub_thread_id": "<reviewer_sub_thread_id>",
 "message_id": "<new uuid>", "is_playground": true, "response_format": {"type": "default"},
 "configuration": {"prompt": "<base + REVIEWER_JSON_TEMPLATE>", "stream": "<streamer present>", "conversation": "<prior reviewer pairs>"},
 "maximum_iterations": "<reviewer cfg value or 10>", "images": [], "files": [], "tools_call_data": [], "usage": {}}
```

`is_playground: True` stops the reviewer's intermediate `sendResponse` from leaking to the client. Because that branch of `execute()` skips history, `_call_reviewer` builds `historyParams` itself with `prepare_history_params`.

Persistence (owned by [queues-history-metrics](queues-history-metrics.md); tables in [data-model](../data-model.md)):

| Row | thread / sub_thread | Tokens | `AiConfig.review_meta` |
|---|---|---|---|
| Main agent (published by `process_background_tasks`) | the main ones | summed over all main attempts (but see the TODO in Cost accounting) | `{rounds, passed}` |
| One per reviewer round (published by `_publish_reviewer_round`) | main `thread_id` + `reviewer_sub_thread_id`; `parent_id` = main bridge id | that round only | `{round_number, passed, target_main_message_id, error?}` |

- Each reviewer round is its own `save_history` message. The Node consumer reads only `historyEntries[0]`.
- The **final** recorded round sends the full `make_request_data_and_publish_sub_queue` payload for the reviewer frame, so `validateResponse`, `broadcast_response_webhook`, `save_agent_memory` and the rest fire once per turn for the reviewer.
- Intermediate rounds, and a final round with no `parsed_data` (hard failure), send only `{"save_history": [row]}`.
- `metrics_data` goes to the metrics queue for every round.
- A failed round gets `success=False` and `error` on its dataset entry. That is what `src/db_services/metrics_service.py:build_history_and_metrics_payload` turns into the row's `error` column.

## Cost accounting

What the code intends: all main attempts are summed into `parsed_data["usage"]`, and each reviewer round is costed on its own row. The reviewer cost comes from `token_calculator.calculate_total_cost` in `_call_reviewer`. `run_review_loop` accumulates reviewer tokens only into the discarded summary. After the loop, the caller sets `result.response.usage.cost` to the summed `expectedCost`.

> [!TODO] Needs a human: the non-streaming path throws the summed totals away. After the loop, `src/services/commonServices/common.py:699-701` calls `update_usage_metrics(parsed_data, params, latency, result=result, success=True)` again. That call recomputes `inputTokens`/`outputTokens`/`total_tokens` from the final attempt's `response.usage` and `expectedCost` from `parsed_data["tokens"]`, which `_rerun_main_agent` set to the last re-run's cost only. So after any re-run, the HTTP response cost, the main history row and the wallet billing events (`src/services/commonServices/baseService/utils.py:compute_billing_events` bills `parsed_data["usage"]`) all show only the last attempt. That second call came in with ba837ed1 "feat: playground history (#1488)", which merged the playground and non-playground tails. The comment above the loop says the second call was meant to be gone. The streaming path has no second call and keeps the sums. Should the second call be removed or skipped when a review ran?

> [!TODO] Needs a human: reviewer calls are never billed or counted against limits. The reviewer's `parsed_data` built in `_call_reviewer` copies none of `wallet`, `charge_hit_fee`, `billing_attribution` or `org_billing_plan`. So `compute_billing_events` emits no events for it, even though `src/services/billing/billing_utils.py:reserve_credits_and_api_key_setup` set `wallet` on the reviewer agent's cfg and checked its model against the plan. Spend limits (`src/services/utils/update_and_check_cost.py:update_cost`) read `parsed_data["tokens"]`, which holds the last main attempt only, on both paths. Round 1 and the reviewer are never added. Is free reviewing intended?

Token counts (not cost) for re-runs and reviewer calls come from the formatter's `response.usage`. Round 1 instead uses `token_calculator.get_total_usage()`, so counts can be low when a re-run looped through tools. Cost is unaffected.

## Gotchas

> [!WARNING]
> **Prompt beats agent.** If both `reviewer_prompt` and `reviewer_agent` are set, the reviewer runs on the *main* agent's model and key. The reviewer agent's config is ignored, but its id is still written as the reviewer rows' `bridge_id`, and its `version_id` is still used.

> [!WARNING]
> **Unparseable verdicts trigger a re-run.** If the reviewer replies with something that is not JSON, the main agent gets the literal text `<reviewer output was unparseable>` as its next user message. Keep reviewer models able to follow JSON instructions.

> [!WARNING]
> **`parsed_data["user"]` stays set to the last reviewer reason after a re-run.** The main history row is safe, because it uses `original_user`. But `process_background_tasks` → `make_request_data_and_publish_sub_queue` reads `parsed_data["user"]` for `save_agent_memory`, `handle_gpt_memory`, `chatbot_suggestions` and `broadcast_response_webhook.user_question`. `configuration.conversation` also keeps the review pairs, and those feed GPT-memory `pending_turns`.

> [!WARNING]
> **Post-tool runs after the review.** The reviewer approves the response *before* `handle_post_tool`, so a configured post-tool can still replace the content the reviewer approved.

> [!TODO] Needs a human: the webhook reviewer looks broken in three ways.
> 1. When the webhook call fails, `axios_work` returns `response` as a string. `run_review_loop`'s soft-failure branch (`src/services/commonServices/reviewer_service.py:228-233`) then calls `.get("error")` on that string. The resulting `AttributeError` is outside the loop's `try`, so the whole request fails even though the main agent succeeded.
> 2. `_call_tool_reviewer` returns `{}` (not `None`) as its `parsed_data`. `_publish_reviewer_round` therefore calls `_attach_sub_thread_extras(row, {})`, which raises at `src/services/utils/common_utils.py:930` (`None.get`). The exception is caught and logged, and **no webhook reviewer row is ever saved**.
> 3. The rows' `bridge_id` is the raw apicall id from `reviewer_tools[0]`, not an agent id.

> [!TODO] Needs a human: an LLM reviewer combined with `reviewer_tools` sets `configuration["tools"]` to `[{"script_id", "url", "title", "_id"}]`. Nothing builds `reviewer_tools_mapping` (`getConfiguration` never sets it). `src/services/commonServices/baseService/utils.py:tool_call_formatter` reads `tool["name"]`, so the reviewer call probably raises `KeyError`. That ends the loop as a hard failure on round 1. Is "agent/prompt + tools" a supported setup?

- The LLM reviewer runs under a fresh `message_id`, so its tool-count key was never initialised. `decrement_tool_count` returns 0 and tool calling is turned off after one tool round ([tool-calling](tool-calling.md)).
- The loop gate checks only the three `_reviewer_*` keys. Cached responses and the plan-mode planner call (which falls through to the normal path) are reviewed like any other response.

## Guardrails (present but not wired in)

> [!WARNING]
> `guardrails_check` and `validate_with_guardrails` have **no callers** in this repo. 7eb10a6d (2026-05-01, which reached master in 9fb5a977 "Testing (#1511)") removed the only call, from `chat()`. Before that, `chat()` ran it before execution when `settings.guardrails.is_enabled` was true, and returned the blocked dict as HTTP 200. `README.md:37` and `docs/CHAT_COMPLETION_FLOW.md:188-189` still say guardrails are applied. `src/services/utils/common_utils.py:parse_request_body` still copies `settings.guardrails` into `parsed_data["guardrails"]`, and `getConfiguration` still takes a `guardrails` argument, but neither is read for moderation.

> [!TODO] Needs a human: should guardrails be re-enabled in `chat()` (and in the streaming path), or should `src/services/utils/guardrails_validator.py` and the `guardrails` plumbing be deleted? The removal commit gives no reason. The same commit also changed the validator from fail-open to fail-closed.

What the code would do if called:

```json
"settings": { "guardrails": {
  "is_enabled": true,                       // read by the old call site, not by guardrails_check
  "guardrails_configuration": {"toxicity": true, "bias": false},   // keys of GUARDRAILS_TEMPLATES
  "guardrails_custom_prompt": "<extra rules>"
} }
```

- It moderates only `parsed_data["user"]`, the input. Some categories (hallucination, data leakage) describe outputs, but the output is never checked.
- Categories are the keys of `GUARDRAILS_TEMPLATES`. If none are enabled, **all** of them are checked.
- It makes one `gpt-5-nano` call with `response_format: json_object`, on the platform `OPENAI_API_KEY` (`config.py`). The customer's key is not used and the call is not billed. With no key it logs and returns `None` (allow).
- Return values of `guardrails_check`:
  - `None` means allow.
  - Blocked: `{"success": false, "blocked_by_guardrails": true, "guardrails_reason", "guardrails_confidence", "guardrails_violations", "response": {"data": {"message": {...}}}}`.
  - Validator error: `{"success": false, "status_code": 502, "guardrails_error": true, "error", "error_type", ...}`. This is fail-closed. A model reply that lacks `is_safe`/`reason`/`confidence` is still treated as **safe** (fail-open).
  - Note: `status_code` is just a dict field. The old call site always sent HTTP 200.

## Why it is built this way

- **The review runs after full resolution, on the terminal agent only.** Tools, A2A and fallback are already done, so the reviewer judges what the user would actually get (module docstring, 6bbf046b / b137e325, April 2026).
- **Re-runs disable fallback and transfers** "to keep the blast radius bounded and token accounting honest" (`_rerun_main_agent` docstring).
- **One queue message per reviewer round, with side effects only on the last one.** The Node `saveConversationHistory` reads only `historyEntries[0]`, and firing webhooks and memory per round would duplicate them (`_publish_reviewer_round` docstring).
- **The reviewer keeps its own multi-turn history across rounds**, so it can check whether its earlier feedback was acted on (`reviewer_conversation_pairs` comment).
- **Latency is patched by hand.** The main timer is used up before the loop, so `over_all_time` would read 0. The reviewer measures its own wall-clock time and re-runs restore the pre-loop latency (3f716a47, #1557).
- **Prompt and tool reviewers and the `settings.review_agent` nesting** came from #1662, "prompt and tools in the review agent", which reached HEAD through 115d4642 (#1698, 2026-08-01). Before that the reviewer was agent-only.
- **Reviewer failures never block the answer.** The loop keeps the latest main response on any reviewer or re-run error (the webhook bug above is the exception).

## Changing it safely

- The response tail exists twice: `chat()` and `sse_stream_and_finalize`. Change the gate, the cost refresh and the post-tool ordering in both ([chat-orchestration](chat-orchestration.md)).
- To change the verdict contract, update `REVIEWER_JSON_TEMPLATE`, `parse_reviewer_json` and `_call_tool_reviewer` together. External webhook reviewers (viaSocket flows) depend on the `{passed, reason}` shape and on the POST body keys.
- Do not add per-round side effects. Keep intermediate rounds history-only, or webhooks and memory will fire up to 3 times.
- If you touch billing, pass the billing keys into the reviewer's `parsed_data` and re-check `compute_billing_events` ([billing-and-limits](billing-and-limits.md)).
- There are no tests for this area. To check a change by hand, run a chat with a reviewer prompt that always fails. You should see 3 reviewer rows under one `sub_thread_id`, `review_meta.rounds == 3` on the main row, and (when streaming) `review_phase` events. See [environment](../environment.md) for `OPENAI_API_KEY`.
