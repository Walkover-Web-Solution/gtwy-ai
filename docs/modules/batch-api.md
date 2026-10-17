---
covers: ["src/services/commonServices/*/*batch*.py", "src/services/utils/batch_script.py", "src/services/utils/batch_script_utils.py", "src/schemas/batch_schemas.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Batch API

`POST /api/v2/model/batch/chat/completion` sends N user messages for one agent to a provider's asynchronous batch API (OpenAI, Anthropic, Gemini, Groq, Mistral) and returns at once with a `batch_id`. A cron on the consumer pods polls every open batch every 15 minutes, formats the finished results and POSTs them once to the caller's webhook. It then updates the history rows and publishes metrics. The request path lives in `src/services/commonServices/common.py:batch` ([chat-orchestration](chat-orchestration.md)); this doc covers the provider batch classes and the cron.

| File | Role |
|---|---|
| `src/schemas/batch_schemas.py` | `BatchChatCompletionRequest`: subclasses `CompletionRequest`. It makes `batch` (non-empty `list[str]`) and `webhook` (`WebhookCredModel`: `url` + optional `headers`) required and checks that `batch_variables` has the same length as `batch`. |
| `src/services/commonServices/openAI/openai_batch.py` | `OpenaiBatch.batch_execute`: builds JSONL lines for `/v1/responses` (developer prompt + history + user). Adds the `web_search` built-in tool. Stores Redis state. |
| `src/services/commonServices/anthropic/anthropic_batch.py` | `AnthropicBatch.batch_execute`: builds `{custom_id, params}` requests (`system` + `messages`). |
| `src/services/commonServices/Google/gemini_batch.py` | `GeminiBatch.batch_execute`: builds `{key, request}` JSONL lines (`contents`, `generation_config`, `system_instruction`). Converts history to Gemini `parts` and drops images. |
| `src/services/commonServices/groq/groq_batch.py` | `GroqBatch.batch_execute`: OpenAI-chat JSONL lines for `/v1/chat/completions` (system + history + user). |
| `src/services/commonServices/Mistral/mistral_batch.py` | `MistralBatch.batch_execute`: `{custom_id, body}` JSONL lines (system + history + user). |
| `src/services/commonServices/*/*_run_batch.py` (5 files) | Provider SDK calls: upload/create the batch, `retrieve_batch_status`, download the result files, and `handle_batch_results(batch_id, apikey) -> (results, is_completed)`. |
| `src/services/utils/batch_script_utils.py` | `BATCH_RESULT_HANDLERS` (service to `handle_batch_results`), `is_finalized_batch_item`, `get_batch_result_data` (reads one raw result line per provider). |
| `src/services/utils/batch_script.py` | The cron: `repeat_function` (loop) and `check_batch_status` (one pass over all open batches). |

Code outside the globs that this area depends on:
- the route `src/routes/v2/modelRouter.py:batch_chat_completion`
- `src/services/utils/helper.py:Helper` (`create_service_handler_for_batch`)
- `src/services/utils/common_utils.py:build_service_params_for_batch` and `process_batch_background_tasks`
- `src/db_services/metrics_service.py:create_batch_conversation_logs`
- `src/db_services/conversationDbService.py:find_completed_batch_conversations`
- `src/services/utils/ai_middleware_format.py:process_batch_results` / `Batch_Response_formatter`
- `src/services/cache_service.py:acquire_lock`
- the startup in `index.py:lifespan`

## Which providers support batch

Three code paths must agree. The request schema does not restrict `service`.

| Step | Source of truth | On an unknown service |
|---|---|---|
| Submit | `src/services/utils/helper.py:Helper` (`create_service_handler_for_batch`, `if/elif` on `service_name`) | `ValueError("Unsupported batch service: …")`, which `handle_exceptions` turns into a 400 |
| Poll | `BATCH_RESULT_HANDLERS` in `src/services/utils/batch_script_utils.py` | `get_batch_result_handler` returns `None`. Calling it raises `TypeError`, which is logged, and the batch is retried every poll until its Redis key expires |
| Parse one result line | `get_batch_result_data` (`if/elif` per service) | `message_id` is unbound, so `UnboundLocalError` |

Today all three cover `openai`, `anthropic`, `gemini`, `groq` and `mistral`.

| Service | Provider call (in `src/services/commonServices/*/*_run_batch.py`) | Our id goes in | In-progress states | Results come from |
|---|---|---|---|---|
| `openai` | `AsyncOpenAI` `files.create` + `batches.create(endpoint="/v1/responses", completion_window="24h")` | `custom_id` | `validating`, `in_progress`, `finalizing`, `cancelling` | `output_file_id` + `error_file_id`, both downloaded in parallel |
| `anthropic` | sync `Anthropic` `messages.batches.create` | `custom_id` | `in_progress`, `canceling` | `messages.batches.results` once `ended` |
| `gemini` | sync `genai.Client` `files.upload` + `batches.create` | `key` | `JOB_STATE_PENDING`, `JOB_STATE_RUNNING` | `batch.dest.file_name` (one file for successes and errors) |
| `groq` | `AsyncGroq` `files.create` + `batches.create(endpoint="/v1/chat/completions")` | `custom_id` | same as OpenAI | output + error files |
| `mistral` | sync `Mistral` `files.upload` + `batch.jobs.create(endpoint="/v1/chat/completions")` | `custom_id` | `QUEUED`, `RUNNING`, `CANCELLATION_REQUESTED` | `output_file` only |

## How it works

### The provider batch / run_batch split

- `<provider>_batch.py` holds one class `<Provider>Batch(BaseService)` with a single method `batch_execute`. It runs inside the HTTP request. It reuses `BaseService` for its constructor (`batch`, `webhook`, `batch_variables`, `processed_prompts`, `customConfig`, `apikey`, …) and for `service_formatter` (maps our config keys to the provider's). It never runs the chat loop: no `execute()`, no tool calls, no fallback.
- `<provider>_run_batch.py` is plain module functions that talk to the provider SDK. The submit class imports its create/upload functions. The cron imports only `handle_batch_results`, through `BATCH_RESULT_HANDLERS`. Keep SDK details here, so the cron never needs to know a provider's status names.

### Submit (request path)

1. `auth_and_rate_limit` and `add_configuration_data_to_body(BatchChatCompletionRequest)` validate the body and resolve the agent ([agent-configuration](agent-configuration.md)). The wallet gate is called with `is_batch=True` (see [Billing](#billing)).
2. `src/services/commonServices/common.py:batch` parses the body. `manage_threads` sets a new `uuid1` thread when none is sent, so all items share one `thread_id`. For each item it runs `Helper.replace_variables_in_prompt` with that item's `batch_variables` (or the plain prompt for all items) and sends one alert listing the missing variables. It loads the model config and runs `handle_pre_tools` once for the whole batch. Then it runs `configure_custom_settings`, deletes `tools`, and runs `validate_json_schema_configuration` and `normalize_response_type`.
3. `build_service_params_for_batch` pops `stream` (a "temporary fix for batch - stream bug", c4333b47). Then `Helper.create_service_handler_for_batch(...).batch_execute()` runs.
4. `batch_execute` (every provider follows the same steps):
   1. If `thread_id` is set, load history with `find_completed_batch_conversations(limit=3)`: the last 3 `conversation_logs` rows with `status = true` whose `batch_data->>'status'` is not `queued`. Convert them with the provider's `ConversationService.create*Conversation`. Every item gets the same history, and items in one batch never see each other.
   2. For each message, make `message_id = uuid4()` and build the provider line with `processed_prompts[idx]` as the system prompt.
   3. Upload and create the batch, then `store_in_cache(f"{redis_keys['batch_']}{id}", batch_json, ttl=86400)`.
   4. Return `{success, message, batch_id, messages, ai_config_mapping}`.
5. `process_batch_background_tasks` → `create_batch_conversation_logs` publishes `{"save_batch_history": [...]}` on the log queue: one `conversation_logs` row per item, `status: false`, `batch_data.status: "queued"`. gtwy-node bulk-inserts them ([queues-history-metrics](queues-history-metrics.md)).

### Poll and deliver (the cron)

`index.py:lifespan` starts `repeat_function()` only when `CONSUMER_STATUS` is `"true"` ([environment](../environment.md)), so only consumer pods poll. The loop:

```python
while _globals.is_ready:      # False after SIGTERM (index.py:_handle_sigterm)
    await check_batch_status()
    await asyncio.sleep(900)  # 15 min
```

On shutdown the lifespan also cancels `batch_task`. One `check_batch_status` pass:

1. `find_in_cache_with_prefix(redis_keys["batch_"])`: Redis `KEYS AIMIDDLEWARE_<ENV>_nd_batch_*`, then a `GET` on each key.
2. For each batch, in sequence:
   1. Take `acquire_lock(batch_id)` (`SET lock_<id> NX EX 1800`). Skip the batch if another pod holds it.
   2. Re-read the cache key (double-checked locking). If it is gone, another pod has finished the batch.
   3. Call `handle_batch_results(batch_id, apikey)`. If it returns `(None, False)`, the batch is still running: log it and move on.
   4. If it is complete with results:
      1. Compute each item's cost: `TokenCalculator.calculate_usage` + `calculate_total_cost`, times `0.5`.
      2. Run `process_batch_results` (in `ai_middleware_format.py`).
      3. If any item fails `is_finalized_batch_item`, `continue`: the key stays, so the batch is retried next poll.
      4. Otherwise `delete_in_cache` the key first, then call `sendResponse` once to the webhook.
      5. Publish `{"update_batch_history": [...]}` (log queue) and `{"save_metrics": [...]}` (metrics queue).
   5. If it is complete with no results, delete the key and send a `no_results` error webhook.
   6. `release_lock` always runs in `finally`.

`is_finalized_batch_item` treats an item as final when it has `status_code >= 400`, or an `error`, or `data.content` is not `None` with `data.finish_reason` in `completed | truncated | tool_call`.

### Partial results and failures

- **Per item.** `get_batch_result_data` flags an item as failed in these cases. A failed item becomes `{message_id, batch_id, error, status_code}` and is delivered alongside the successes.
  - openai/groq: `response.status_code >= 400` or `body.error`
  - anthropic: `result.type == "errored"`
  - gemini and mistral: `response.error`
- **Whole batch.** A terminal state with no result lines (failed, expired, cancelled, Mistral `TIMEOUT_EXCEEDED`, unknown) makes the handler return one pre-built item `{error: {message, type, batch_status}, status_code: 400}`. `process_batch_results` passes it through (it has no `custom_id`), and the webhook gets `success: false`.
- **Partial.** OpenAI, Groq, Gemini and Mistral download whatever result file exists, even for failed/expired/cancelled batches, so partial results are delivered.
- **OpenAI `completed` with no files** returns `(None, False)` and keeps polling (b55a70f5). Groq, Gemini and Mistral send a `no_results` error in the same situation.

## Contracts

Request body (only the batch-specific fields; the rest is `CompletionRequest`, see [api](../api.md)):

```json
{
  "agent_id": "<bridge id>",                       // or bridge_id / version_id
  "batch": ["message 1", "message 2"],            // required, >= 1 string
  "webhook": {"url": "https://…", "headers": {"x-key": "…"}},  // required, extra keys forbidden
  "batch_variables": [{"name": "A"}, {"name": "B"}],  // optional, len == len(batch)
  "thread_id": "…", "sub_thread_id": "…",          // optional; history source
  "meta": {}                                       // optional; echoed to the webhook
}
```

Synchronous 200 response (`common.batch`). Errors are a 400 from `handle_exceptions`:

```json
{"success": true, "response": "Response will be successfully sent to the webhook within 24 hrs.",
 "batch_id": "<provider id>", "messages": [{"message": "message 1", "message_id": "<uuid4>", "variables": {}}]}
```

Webhook POST (`sendResponse` with `create_response_format(url, headers)`):

```json
{
  "response": [                       // key is "error" when NO item succeeded
    {"data": {"id": "…", "content": "…", "finish_reason": "completed", "annotations": null, "model": "…"},
     "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "cost": 0.00012},
     "batch_id": "…", "message_id": "<uuid4>", "isBatch": true, "variables": {}},
    {"message_id": "<uuid4>", "batch_id": "…", "error": {}, "status_code": 400}
  ],
  "success": true,
  "variables": {},
  "meta": {}                          // only when the request had meta
}
```

The caller matches results to inputs by `message_id` (returned in `messages` at submit time). Result order is the provider's, not the input order.

Redis state, `nd_batch_<provider batch id>` (with the `AIMIDDLEWARE_<ENV>_` prefix), TTL 86400 s. Common keys:

```json
{"id": "<provider batch id>", "apikey": "<decrypted provider key>", "webhook": {"url": "…", "headers": {}},
 "batch_variables": null, "message_id_mapping": {"<uuid4>": 0}, "service": "openai", "model": "…",
 "org_id": "…", "bridge_id": "…", "version_id": "…", "thread_id": "…", "meta": null}
```

Each provider also stores its submit-time status snapshot (`status` / `processing_status` / `state`, `created_at`, `request_counts`, `uploaded_file` / `uploaded_file_id`). The cron does not read any of these.

Queue messages the cron publishes:
- `update_batch_history: [{batch_id, message_id, update_data}]`. `update_data` holds `llm_message`, `chatbot_message`, `status`, `error`, `finish_reason`, `annotations`, `tokens: {input_tokens, output_tokens, total_tokens, expected_cost}` and `batch_data: {status: "completed", batch_id, webhook_response, webhook_error, webhook_url, webhook_headers}`, with the headers masked by `Helper.mask_headers`. gtwy-node updates the row `WHERE message_id = … AND batch_data->>'batch_id' = …`.
- `save_metrics`: one row per item with `latency: 0`, `apikey_id: ""` and `time_zone: "Asia/Kolkata"`.

`handle_batch_results` contract (for a new provider): return `(None, False)` while running. Return `(list_of_raw_lines, True)` when there are results. Return `([{error: {...}, status_code: 400}], True)` for a terminal state with no lines. Never raise for "not ready yet".

## Billing

Confirmed: batch calls are never charged to the wallet.
- `reserve_credits_and_api_key_setup` (`src/services/billing/billing_utils.py`) gives an agent without its own key the platform key (`get_platform_apikey`) and `wallet=True`, the same as chat. It also runs the plan allowlist, so a model outside the org's plan is still rejected. But it returns before `reserve_credits` when `is_batch` is set: no hold and no balance check. Per its docstring, nothing on the batch path would release a hold, so one placed there could only leak.
- Neither `common.batch` nor the cron calls `make_request_data_and_publish_sub_queue` / `compute_billing_events`. The cron publishes only `update_batch_history` and `save_metrics`, never a `billing` key, so no usage debit and no per-hit fee are sent.
- `wallet` is not passed into `build_service_params_for_batch` or stored in `batch_json`. The cron cannot tell a platform-key batch from an own-key one.
- The `cost` it computes (`total_cost * 0.5`) goes only into the webhook `usage`, `tokens.expected_cost` and metrics.

See [billing-and-limits](billing-and-limits.md).

> [!TODO] Needs a human: is free batch on the platform key intended? An org with an empty wallet can run unlimited batch traffic on the platform key today. If not, store `wallet` and `billing_attribution` in `batch_json` and emit billing events from the cron.

## Gotchas

> [!WARNING]
> **One non-final item blocks the whole batch, and it ends silently.** If any formatted item fails `is_finalized_batch_item`, nothing is sent and the batch is retried every 15 minutes until its 24 h Redis key expires. Then it disappears: no webhook, and the history rows stay `queued`. Cases that fail the check:
> - `finish_reason` mapping to `other`. In `src/services/utils/formatters/finish_reason.py` that includes Anthropic `stop_sequence`/`refusal`, Gemini `SAFETY`, and OpenAI `incomplete` with reason `content_filter`. <!-- verify:ignore -->
> - `content` of `None`.
> - Anthropic result types `canceled`/`expired`. `get_batch_result_data` only treats `errored` as an error, so these format as an empty success.

> [!WARNING]
> **The Redis TTL (86400 s) is no longer than the provider windows.** OpenAI/Groq batches use a 24 h window, and the Gemini handler's own message says jobs expire after 48 h. A batch that finishes near or after 24 h has no Redis key left, so its results are never collected or delivered. The TTL is also the only cleanup for stuck batches.

> [!WARNING]
> **The webhook is sent once, with no retry.** The key is deleted before `sendResponse`, so that a second pod cannot re-send after the lock expires. A slow or failing webhook therefore loses the delivery. `send_request` catches every exception and returns `{"error", "details"}`, so `webhook_error` in `batch_data` is effectively always `None`. Check `batch_data.webhook_response` instead. The results survive only in the `conversation_logs` rows.

> [!WARNING]
> **Whole-batch errors never update history.** The pre-built terminal-error item has no `message_id`, so the `update_batch_history` loop skips it and every row of that batch stays `batch_data.status = "queued"`, `status = false`.

> [!WARNING]
> **The decrypted provider key sits in Redis in plain text** inside `nd_batch_*` for up to 24 h. This includes the platform key for wallet agents. `clear_cache` in `src/services/cache_service.py` would also wipe all open batches.

> [!WARNING]
> **Sync SDK clients block the event loop.** The Anthropic, Gemini and Mistral `src/services/commonServices/*/*_run_batch.py` functions are `async def` but call sync clients (`Anthropic`, `genai.Client`, `Mistral`), including the Anthropic results iterator. On a consumer pod this stalls the queue consumer while a large batch downloads. OpenAI and Groq use async clients.

> [!WARNING]
> **A failed Redis write is silent.** `store_in_cache` logs the error and returns `False`, and `batch_execute` ignores the result. The caller gets `success: true` and a `batch_id`, the provider runs and bills the batch, and the cron never sees it. Also, `find_in_cache_with_prefix` uses `KEYS` and `json.loads(await client.get(key))`. If a key expires between the two calls, `json.loads(None)` raises, the function returns `None`, and that whole poll is skipped.

> [!WARNING]
> **No consumer pod means no results.** The cron runs only where `CONSUMER_STATUS` is `"true"`. Batches are accepted on any pod but are only collected by consumer pods.

> [!TODO] Needs a human: `common.batch` reads `result['historyParams']` whenever `pre_tool_response_to_save` is set, but no `batch_execute` returns that key. An agent with any pre-tool therefore raises `KeyError` after the provider batch is created and stored in Redis. The caller gets a 400, the "queued" history rows are never written, and the cron still delivers to the webhook. Should this branch be removed from `batch`?

> [!TODO] Needs a human: for `mistral`, `get_batch_result_data` takes the completion from `response`, but for `openai`/`groq` it takes it from `response.body`. If Mistral's output lines use the OpenAI shape (`response.status_code` + `response.body`), every Mistral item formats with `content: None` and never finalizes. The `mistral` case in `TokenCalculator.calculate_usage` would also hit `KeyError` on `usage`. Has a Mistral batch been delivered end to end?

Also note:
- `tools` are always dropped. The only tool a batch can carry is the OpenAI `web_search` built-in.
- Gemini history drops image parts.
- For `reasoning_model` (o1-preview/o1-mini) the OpenAI batch sends no developer prompt at all.

## Why it is built this way

- **15-minute poll and 30-minute lock.** c5036ca3 "fixed the mistaken batch cron timing" raised the sleep from 15 s to 900 s. a0dcdee2 raised the lock TTL to 1800 s. The `acquire_lock` docstring says the TTL must be greater than the poll interval (900 s) plus the processing time.
- **Double-checked locking, delete before webhook.** These come from comments in `check_batch_status`. Another pod may finish and delete the key between `KEYS` and the lock, and deleting first means no pod can pick the batch up again after the lock expires. The cost is at-most-once delivery.
- **`is_finalized_batch_item`.** Added in d67e5d94 ("batch api bug") so a batch is delivered only when every item is final.
  > [!TODO] Needs a human: which provider returned non-final items that made this necessary? The retry-until-expiry side effect seems to be unintended.
- **OpenAI `completed` with no files keeps polling** (b55a70f5). There is no reason on record. Presumably the files can appear after the status flips.
- **`* 0.5` cost.** Added in d3361409 with no comment. It assumes a flat 50% batch discount for every provider.
  > [!TODO] Needs a human: confirm the per-provider batch discount, Groq and Mistral in particular.
- **Validation in Pydantic.** The per-provider `batch_variables` checks were removed in 38aacc53 when `BatchChatCompletionRequest` took them over. Keep validation in the schema.
- **One Redis prefix.** a268cecc fixed a "redis key mismatch": the cron searched `"batch_"` while writers used `redis_keys["batch_"]` (`nd_batch_`). Read and write must both use `redis_keys["batch_"]`. The `nd_` prefix means "no delete" ([data-model](../data-model.md)).
- **History excludes queued rows.** `find_completed_batch_conversations` filters out `batch_data.status = queued`, so pending placeholders ("Your message has been queued…") are never fed back as context.
- **Python publishes, Node writes.** History rows are inserted and updated by gtwy-node from the log queue. The `conversation_logs.batch_data` column is a gtwy-node migration (`models/postgres/pg_models.py:ConversationLog` mirrors it).

## Changing it safely

- **New batch provider.** Add `<p>_batch.py` + `<p>_run_batch.py` in the provider folder. Register the class in `Helper.create_service_handler_for_batch`, the handler in `BATCH_RESULT_HANDLERS`, and a branch in `get_batch_result_data` (where the id and body live, and what counts as an error). Make sure the formatter in `src/services/utils/formatters/` has a batch branch (only Gemini and Anthropic take `isBatch`), and that its finish reasons map to `completed`/`truncated`/`tool_call`, or items never finalize. See [llm-providers](llm-providers.md) and [recipes](../recipes.md).
- **Poll interval.** If you change `asyncio.sleep(900)`, keep the `acquire_lock` default TTL above interval + worst-case pass time.
- **Redis TTL.** If you raise the `ttl=86400` in the five `batch_execute` methods, change all five together. Pick a value above the longest provider window plus one poll.
- **Webhook payload.** Customers parse `response`/`error` and `message_id`. The `update_batch_history` keys must match columns in gtwy-node `conversation_logs`.
- **Checking it.** There are no tests for this area. Submit a 2-item batch against a test agent and watch the logs: "Batch Script running...", then "still in progress", then "webhook sent". Then check the webhook receiver and the two `conversation_logs` rows (`batch_data.status` goes from `queued` to `completed`). Lowering the sleep locally is the quickest way; restore it after.
