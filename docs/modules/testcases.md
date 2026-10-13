---
covers: ["src/services/testcase_service.py", "src/controllers/testcase_controller.py", "src/services/commonServices/testcases.py", "src/db_services/testcase_services.py", "src/services/utils/testcase_utils.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Testcases (agent evaluation) and reruns

"Testcases" here is a product feature, not this repo's test suite (the repo has no automated tests). A testcase is a saved conversation plus an expected answer for one agent. `POST /api/v2/model/testcases` replays saved testcases against one or more agent versions (and optionally other models), scores each answer, and streams the results to the caller over RTLayer. `POST /api/v2/model/rerun` is a different feature: it re-sends past production messages from Postgres through the queue, with no scoring. Both are documented here because they are easy to confuse.

| File | Role |
|---|---|
| `src/services/testcase_service.py` | The runner. Validates the request, loads testcases, builds one config per (version, model) pair and runs the credit gate for it, calls `chat` once per testcase in parallel, publishes RTLayer events. Entry: `execute_testcases`. |
| `src/services/commonServices/testcases.py` | Scoring. `compare_result` (exact / cosine / ai) and `process_single_testcase_result`, which `chat` calls after the answer is ready. |
| `src/controllers/testcase_controller.py` | `handle_playground_testcase`: saves a playground answer as a new testcase or refreshes an existing one. The three CRUD controllers in this file have no route (see Gotchas). |
| `src/db_services/testcase_services.py` | Mongo `testcases` access. `update_testcase_last_executed` stamps runs. `get_testcases` and `parse_and_save_testcases` have no callers. |
| `src/services/utils/testcase_utils.py` | Dead code: per-provider message builders (`add_prompt_and_conversations`) with no callers. |

Code outside these files that belongs to the feature:

| Where | What |
|---|---|
| `src/routes/v2/modelRouter.py:run_testcases_route` | HTTP entry; starts `execute_testcases` as a background task. |
| `src/routes/v2/modelRouter.py:rerun_messages_route` | HTTP entry for reruns. |
| `src/services/commonServices/common.py:chat` | Calls `process_single_testcase_result` when `testcase_data.run_testcase` is true and stamps the score onto `historyParams`. |
| `src/services/utils/common_utils.py:process_background_tasks` | Calls `handle_playground_testcase` for playground requests that carry `testcase_data`. |
| `src/services/commonServices/baseService/utils.py:get_saved_tool_response` | Replays saved tool responses during a testcase run. |
| `src/services/utils/helper.py:queue_rerun_messages`, `src/db_services/conversationDbService.py:find_rerun_logs` | Rerun: read past logs, build queue messages. |

## How it works

### Running testcases (`POST /api/v2/model/testcases`)

1. `run_testcases_route` runs `auth_and_rate_limit` (JWT, 100/bridge, 20/thread). It does **not** use `add_configuration_data_to_body`, so there is no middleware config load and no middleware credit hold. It requires `bridge_id`, builds the RTLayer channel `{org_id}_{bridge_id}_{user_id}` with `build_rtlayer_cred`, starts `execute_testcases` with `asyncio.create_task`, and returns at once.
2. `validate_testcase_request_data` normalises the body (see Contracts). `fetch_testcases_from_request` picks the testcases, first match wins:
   1. `testcases: true` plus `testcase_data` → one in-memory testcase with `_id: "direct_testcase"` and `type: "response"`. Not saved.
   2. `testcase_ids` → those docs, in the order given.
   3. `testcase_id` → that one doc.
   4. Otherwise every doc in `testcases` with this `bridge_id`.
   None found → `TestcaseNotFoundError`.
3. Event `run_started` is published.
4. For every `version_id` × every entry of `models` (or once per version when `models` is empty), `run_for_version_model` runs concurrently:
   1. `_resolve_override` turns the model entry (or the top-level `service`/`model`) into `{service, model, is_overridden: True}`, or `None`.
   2. `get_testcase_configuration` calls `getConfiguration` with `version_id` (bridge id is `None` in direct mode), takes the primary agent's cfg, applies the override with `_apply_model_override`, then runs `reserve_credits_and_api_key_setup` (see [Billing](#billing-and-credits)).
   3. The hold token is popped off the cfg, `run_testcases_parallel` runs, and the hold is released in `finally`.
5. `run_testcases_parallel` calls `process_single_testcase` for every testcase with `asyncio.gather`, each on a `copy.deepcopy` of the cfg. There is no concurrency cap: testcases × versions × models all run at once. Afterwards it sets `execution.lastExecutedAt` and `updatedAt` on the Mongo docs (`update_testcase_last_executed`; `direct_testcase` is skipped).
6. `process_single_testcase` builds a fresh chat body and calls `chat` directly (not over HTTP, not through the queue):
   - variables: cfg variables (from the request) overlaid by the testcase's own `variables` (testcase wins);
   - `configuration.conversation` = the testcase `conversation`; `user` = content of its last entry;
   - `configuration.stream = False` and `settings.fall_back.is_enable = False`;
   - `user_urls` from the testcase, plus its `pdf`/`file` URLs passed explicitly as `files` (placed after `**cfg` so the cfg cannot overwrite them);
   - `org_id` stamped explicitly, `bridge_configurations` = only `{primary_bridge_id: cfg}`;
   - `testcase_data` = `{matching_type, run_testcase: True, _id, expected, type, skip_testcase_creation: True, is_overridden, tools_response}`.
7. Inside `chat`, tools listed in `tools_response` are not called; `get_saved_tool_response` returns the first recording whose `args` equal the call's args exactly, else an error payload the model sees as a tool failure. Tools not listed run for real.
8. After the answer (and reviewer, post-tool and `sendResponse`), `chat` calls `process_single_testcase_result`, adds a per-testcase latency (it uses `model_execution_time`, not `over_all_time`), puts the result at `response.testcase_result`, and copies score fields to `historyParams.testcase_id` / `historyParams.testcase_data`. The normal history publish then writes them onto that run's Postgres `conversation_logs` row (columns `testcase_id`, `testcase_data`, written by gtwy-node from the log queue).
9. Back in `process_single_testcase`, the JSON body is read and one outcome is built and published as `testcase_result`. A `success: false` body from `chat` becomes a failed outcome with `score: 0`.
10. When every pair is done, `execute_testcases` publishes `run_completed` with the final payload. The route ignores the return value.

### Scoring (`compare_result`)

`process_single_testcase_result` takes `actual` = `response.data.content` and `expected` = `expected.response` when `type == "response"`, else `expected.tool_calls`. `matching_type` comes from the request override, then the testcase, then `"cosine"`.

| `matching_type` | How | Score |
|---|---|---|
| `exact` | `pydash.is_equal(expected, actual)` | `1` or `0` |
| `cosine` | `src/services/utils/nlp.py:compute_cosine_similarity`: TF-IDF fitted on just the two strings | float 0..1 |
| `ai` | AI judge: `src/services/utils/ai_call_util.py:call_ai_middleware` with `user = str(actual)` and variables `{expected, ai_matching_custom_prompt?, bridge_summary?}` to the platform agent `bridge_ids["compare_result"]` in `src/configs/constant.py` | whatever the judge returns as `score`, plus `reason` |
| anything else | no branch | `None` |

A scoring error (judge call failed, bad `expected` shape) does not fail the run: `process_single_testcase_result` catches it and returns `score: 0`. The RTLayer outcome still says `success: true`; the error text is only in `conversation_logs.testcase_data.error`. A failed judge call shows up there as "'str' object has no attribute 'get'", because `call_ai_middleware` returns the error string in `response`.

`ai_matching_custom_prompt` comes from the agent doc's `agent_info.ai_matching_custom_prompt` and `bridge_summary` from the agent doc, both via `getConfiguration` and `parse_request_body`. The judge's prompt lives in that platform agent, not in this repo.

### Saving a testcase from the playground

A normal `POST /api/v2/model/chat/completion` whose body has a non-empty `testcase_data` (without `skip_testcase_creation`) is a "save as testcase" request. In `process_background_tasks`:

- `testcase_data.testcase_id` present → background `handle_playground_testcase(..., Flag=False)` updates that doc: `expected = {"response": <this answer>}`, `conversation` = the request's `configuration.conversation` plus `{"role": "user", "content": <user>}`, `matching_type` if sent, `user_urls` only if this run had attachments.
- No id → a new ObjectId is put in `response.testcase_id` (so the HTTP response carries it), `testcase_data` is sent to RTLayer channel `{org_id}_{bridge_id}_{version_id}`, and the doc is created in the background with that `_id`, defaults `type: "response"`, `matching_type: "exact"`.

### Reruns (`POST /api/v2/model/rerun`)

1. `add_configuration_data_to_body(schema_class=None)` loads the agent named in the body (current config, not the version in the old log) and places a normal credit hold. It skips the "user message is compulsory" check when `message_ids` or `thread_id`+`sub_thread_id` are present.
2. `rerun_messages_route` pops `credit_hold_token` from the body and releases it in `finally`, queued or not.
3. `find_rerun_logs` reads Postgres `conversation_logs`, always filtered by `org_id`:
   - `message_ids` → those rows (any agent of the org; `bridge_id` is not checked);
   - `thread_id` + `sub_thread_id` (and no `message_ids`) → the last 6 rows of that thread with a truthy `status`; the newest is rerun and all 6 become history (`configuration.conversation`).
4. `build_rerun_queue_message` copies the request body per log: original `user`, `variables`, `user_urls`; new `message_id`; new thread ids `rerun_<old>_<8 hex>`; `is_rerun: True` plus `original_*` ids; `stream: False`; `response_format` = the old one only if it was a webhook with a URL, else `{"type": "default"}`. The token is stripped from each copy.
5. Each copy is published to the main RabbitMQ queue; the consumer runs `chat_multiple_agents`. The HTTP reply is `{success, message, queued, not_found?, history_count?}`.

| | `/testcases` | `/rerun` |
|---|---|---|
| Input | Mongo `testcases` or inline `testcase_data` | Postgres `conversation_logs` rows |
| Config | each requested `version_id` (× `models`) | the agent config the middleware loads |
| Runs | in-process, `asyncio.gather` → `chat` | RabbitMQ → consumer → `chat_multiple_agents` |
| Scoring | yes | no |
| Results | RTLayer events | none in the reply; new history rows, plus the old webhook if any |
| Tools | replayed if recorded, else real | always real |
| Fallback | disabled | as configured |
| Credit hold | one per (version, model), released in the service | middleware hold, released by the route |

## Contracts

`POST /api/v2/model/testcases` body (read in `validate_testcase_request_data`):

```jsonc
{
  "bridge_id": "<agent id>",          // required by the route (channel name, rate-limit key)
  "version_ids": ["<version id>"],    // required; a single string is wrapped into a list
  "testcase_ids": ["<id>"],           // optional, order kept
  "testcase_id": "<id>",              // optional, single
  "testcases": true,                  // with testcase_data: run one unsaved testcase
  "testcase_data": { "conversation": [], "expected": {}, "matching_type": "cosine",
                     "user_urls": [], "tools_response": {} },  // first three required
  "variables": {},                    // under each testcase's own variables
  "matching_type": "ai",              // optional; overrides every testcase
  "service": "openai", "model": "gpt-4o",          // optional single override
  "models": [{ "service": "anthropic", "model": "…" }]  // optional comparison; each needs both keys; wins over service/model
}
```

HTTP reply (always, once the route accepts the body): `{"success": true, "channel_id": "<org>_<bridge>_<user>", "message": "Your response will be sent through configured means."}`.

RTLayer events on that channel (each message is `{"event": <name>, ...payload}`, passed through `_json_safe`):

| `event` | When | Payload |
|---|---|---|
| `run_started` | testcases loaded | `bridge_id`, `version_ids`, `total_testcases` |
| `testcase_result` | each testcase × version × model | `version_id`, `model`, `service`, `is_overridden`, `message_id` (missing on setup errors), `result` |
| `run_completed` | end of run | the final payload below; or `{success: true, message, results: []}` when no testcases matched |
| `run_failed` | validation error on a plain run, or any crash | `error` |

```jsonc
// result (one outcome)
{ "testcase_id": "<id>|direct_testcase", "bridge_id": "…", "expected": { "response": "…" },
  "actual_result": "…", "score": 0.83, "reason": "…", "matching_type": "cosine",
  "success": true, "tools_call_data": [], "total_tokens": 0, "cost": 0,
  "latency": { "over_all_time": 1.2 /* model time */, "execution_time_logs": {} },
  "model": "…", "service": "…", "is_overridden": false, "message_id": "<uuid1>" }
// failed: actual_result null, score 0, error "<text>", error_code (setup errors only)

// run_completed payload
{ "success": true, "bridge_id": "…", "version_ids": [], "total_versions": 1,
  "version_results": [{ "version_id": "…", "total_testcases": 3, "results": [],
                        "model": "…", "service": "…", "is_overridden": false,
                        "tools_call_data": [] /* from results[0] only */, "total_tokens": 0, "cost": 0 }],
  "testcase_source": "mongodb" | "direct" }
```

Mongo `testcases` document (schema owned by gtwy-node, `src/mongoModel/Testcase.model.js`): <!-- verify:ignore -->

```jsonc
{
  "_id": ObjectId,
  "bridge_id": "<agent id string>",
  "name": "",                                   // Node only
  "conversation": [{ "role": "user|assistant|tools_call", "content": "…" }],  // last entry = the user turn replayed
  "type": "response" | "function",
  "expected": { "response": "…" } | { "tool_calls": [{ "name": "…", "arguments": {} }] },
  "matching_type": "exact" | "cosine" | "ai",  // Node enum
  "variables": {},
  "user_urls": [{ "url": "…", "type": "image|pdf|audio" }],
  "tools_response": { "<tool name>": { "recordings": [{ "args": {}, "response": {}, "recorded_at": "…" }] } },
  "execution": { "lastExecutedAt": Date | null },
  "updatedAt": Date,
  "updated_at": Date                            // only set by Python update_testcase_by_id
}
```

Run history is not in Mongo: every run is a normal Postgres `conversation_logs` row with `testcase_id` and `testcase_data` = `{expected, actual, score, reason, matching_type, type, success, error, system_prompt, model, is_overridden}` (built in `chat`, carried by `src/db_services/metrics_service.py`). See [data model](../data-model.md).

## Billing and credits

- The credit gate runs inside the service, once per (version, model) pair: `get_testcase_configuration` → `reserve_credits_and_api_key_setup(org_id, db_config)`. The override is applied **before** the gate so the key, `wallet` flag, plan check and hold match the model that actually runs. `_apply_model_override` resets `apikey` to the org's own key for the new service (from `service_apikeys`) or `None`; with no own key and no platform key it raises before the gate.
- Confirmed separate release point: `run_for_version_model` pops `credit_hold_token` off the cfg and calls `release_credits(org_id, hold_token)` in its `finally`, after all testcases of that pair finish. The token must never reach the per-testcase chat bodies: `chat` pops and releases any token it finds, so the hold would be returned after the first testcase.
- One flat hold (see [billing](billing-and-limits.md#credit-hold-lifecycle)) covers a whole batch, whatever its size. It is an admission check, not a budget.
- Each testcase's `chat` emits its own debit through the normal `compute_billing_events` path, driven by the per-cfg `wallet` flag. This needs `org_id` in the chat body, which is why `process_single_testcase` stamps it.
- Gate refusal: a plain run fails the whole run (`run_failed`); a comparison model (`models` or `service`/`model`) is reported as one failed outcome per testcase with `error_code` (`CREDIT_BALANCE_EXHAUSTED`, `FREE_PLAN_MODEL_RESTRICTED`), and the other pairs keep running.
- The AI judge call is a separate request to production gtwy as the platform's own agent; its cost is not in the outcome's `cost`.
- Rerun: the middleware hold covers only the `/rerun` request and is released by the route. Queued copies run with no hold, but still carry the `wallet` flags the middleware stamped on `bridge_configurations`, so they are debited.

## Gotchas

> [!WARNING]
> The HTTP reply of `/testcases` is always success. Validation errors (missing `version_ids`, bad ids, no API key, empty wallet) arrive only as `run_failed` on RTLayer, and for a plain run the `error_code` is dropped: the route sends `str(error)` only.

> [!WARNING]
> The channel is `{org_id}_{bridge_id}_{user_id}`. The route's docstring says `{org_id}_{bridge_id}` and lists a `version_completed` event; both are stale (no code publishes `version_completed`; it was dropped before #1559 merged). The playground "save as testcase" push uses a different channel, `{org_id}_{bridge_id}_{version_id}`.

> [!WARNING]
> Tool replay is per tool name. Any tool not in the testcase's `tools_response` runs for real during a testcase run (HTTP tools, webhooks, DB writes). Argument matching is exact dict equality; a mismatch gives the model an error payload, not the real tool.

> [!WARNING]
> Testcase runs keep the version's `settings.response_format` and are not marked `is_playground`. A version configured with a webhook or RTLayer response format also receives every testcase answer there, and failing testcases fire the normal error alerts from `src/handler/executionHandler.py:handle_exceptions`.

> [!WARNING]
> Default `matching_type` differs by path: `cosine` when running, `exact` when the playground creates a testcase, and `contains` in the unused `parse_and_save_testcases` (no scoring branch, so the score would be `None`).

> [!WARNING]
> `testcase_controller.py` still has `create_testcase_controller`, `delete_testcase_controller` and `get_all_testcases_controller`, but no route uses them since ebfddb7e. Testcase CRUD lives in gtwy-node (`src/routes/testcase.routes.js`). Change CRUD behaviour there, not here. <!-- verify:ignore -->

> [!WARNING]
> `body.state.profile` set by `run_testcases_route` is not read downstream; `process_single_testcase` builds a bare `state`. `org_id` reaches `chat` only through the explicit `org_id` key. Do not remove it.

> [!WARNING]
> Rerun errors look like 404s: `find_rerun_logs` returns empty on any database error, and the route then answers "No conversation found".

> [!TODO] Needs a human: `type: "function"` testcases look broken. `compare_result` builds `actual` from `case["args"]` of a list, but `process_single_testcase_result` always passes `response.data.content` (a string), so a non-empty answer makes the scorer throw and the score is `0`. Should `actual` come from `tools_call_data`?

> [!TODO] Needs a human: `process_single_testcase` forwards only the primary cfg in `bridge_configurations`. Connected agents (agent-as-tool via `src/services/utils/ai_call_util.py:call_gtwy_agent`) and a reviewer agent are missing from that map, so agent tools fail and the reviewer agent is skipped in testcase runs. Intended?

> [!TODO] Needs a human: the last `conversation` entry is sent twice, as history (`configuration.conversation`) and again as `user`. Thread-mode reruns do the same (the target row is also in the 6-row history). Intended?

> [!TODO] Needs a human: the `except` branch of `process_single_testcase` reads `model_name`, `service_name` and `is_overridden`, which are assigned only after `chat` returns. Any exception before that (for example a conversation whose last entry is not a dict) becomes `UnboundLocalError`, which escapes `asyncio.gather` and turns the whole run into `run_failed`. (`chat` itself returns a 400 `JSONResponse` through `handle_exceptions` rather than raising, so normal model errors do not hit this.)

> [!TODO] Needs a human: why is fallback disabled for testcase runs (332e015c "fix: default model")? Presumably so the score belongs to the model under test, but there is no record.

## Why it is built this way

- **RTLayer, not the HTTP response** (f299303a): long runs hit HTTP timeouts, so the route returns at once and streams events.
- **Run history in Postgres `conversation_logs`** (cea89f5a): the Mongo `testcases_history` collection was dropped (gtwy-node migrations of 2026-05-19) so run history lives with all other history. Mongo keeps only the testcase and `execution.lastExecutedAt`. <!-- verify:ignore -->
- **Deep copy per testcase** (1f80577c, cea89f5a): a shallow copy of the cfg let parallel testcases overwrite each other's nested config.
- **`stream = False`** (c72e9a50): every version must return a parseable `JSONResponse`, whatever its stream setting.
- **`org_id` stamped and override before the gate** (e4437dee): without `org_id`, `parse_request_body` resolved it to `None`, gtwy-node dropped every testcase debit as malformed, and history rows had no org. Applying the override after the gate made comparison runs fail for wallet-only orgs ("No API key configured"), billed own-key runs to the wallet, and checked the plan against the wrong model.
- **Tool replay** (42533afb): replaying recorded tool responses avoids side effects (DB writes, Sheet updates, webhooks) on every run.
- **Per-testcase latency from `model_execution_time`** (comment in `chat`): `over_all_time` of testcases dispatched together accumulates the time of earlier ones.
- **`user_urls` stored in the `conversation_logs` shape** (dd077f21) so a saved testcase replays its attachments.
- **Rerun hold stripped from copies** (comment in `build_rerun_queue_message`): when each queued copy carried the token, every consumer released the same hold, which gave free credits.
- **Rerun keeps the original webhook** (53ab0647): a rerun of a webhook request answers the same webhook; anything else goes to history only.

## Changing it safely

- New field on a testcase: add it to the gtwy-node schema and Joi validation, to the direct-mode object in `fetch_testcases_from_request`, and to the chat body in `process_single_testcase`. Fields the node schema does not declare are dropped by Mongoose on Node writes.
- New `matching_type`: add a `case` in `compare_result` and the value to the gtwy-node enum; the frontend (not in either repo) offers the choices.
- Event or outcome keys: the frontend listens on RTLayer. Treat `run_started` / `testcase_result` / `run_completed` / `run_failed` and the outcome keys as an external contract.
- Persisted run fields: `historyParams.testcase_data` in `chat`, `src/db_services/metrics_service.py`, and gtwy-node's history saver and `conversation_logs` columns must change together. See [queues, history and metrics](queues-history-metrics.md).
- Billing: keep one `reserve_credits_and_api_key_setup` per (version, model) and one release per token; never let `credit_hold_token` reach a chat body; any new service/model swap must happen before the gate. See [billing](billing-and-limits.md).
- Anything shared with normal chat (prompt build, tools, history) is in [chat orchestration](chat-orchestration.md) and [tool calling](tool-calling.md); the config load is in [agent configuration](agent-configuration.md).
- Check by hand: call `POST /api/v2/model/testcases` with a JWT, subscribe to the returned `channel_id`, and confirm `run_started` → `testcase_result` per testcase → `run_completed`; then find the rows in `conversation_logs` by `message_id` and check `testcase_id` and `testcase_data`. For wallet orgs, confirm the shadow balance returns to its start minus the debits once the run ends.
