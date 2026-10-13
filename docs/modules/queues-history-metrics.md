---
covers: ["src/services/commonServices/queueService/**", "src/db_services/conversationDbService.py", "src/db_services/metrics_service.py", "src/db_services/orchestrator_history_service.py", "src/db_services/webhook_alert_Dbservice.py", "src/send_alert.py", "src/utils/**", "src/services/cache_service.py", "src/services/cache_utils.py", "src/handler/**", "src/services/grafana.py", "src/configs/constant.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Queues, history and metrics

The plumbing under every completion: three RabbitMQ queues, the payload builders that turn a finished call into a conversation-log row and metrics rows, the Redis helpers that every other module uses, internal error alerts, and the `handle_exceptions` decorator that turns any error (including DB/Redis timeouts) into a 400. This repo **publishes** history and metrics; gtwy-node (the sibling repo) **writes** them to Postgres and Timescale. Python only reads history back.

The full table and key catalogue is in [data-model](../data-model.md); queue and Redis env vars are in [environment](../environment.md).

| File | Role |
|---|---|
| `src/services/commonServices/queueService/baseQueue.py` | `ConnectionManager` (one robust connection, one channel per queue) and `BaseQueue` (declare, publish with retry, consume wrapper, `-Failed` queue). |
| `src/services/commonServices/queueService/queueService.py` | Main request queue `Queue` / `queue_obj`. The only queue this repo consumes. |
| `src/services/commonServices/queueService/queueLogService.py` | Log queue `Queue2` / `sub_queue_obj` (history + side jobs). Publish only. |
| `src/services/commonServices/queueService/queueMetricsService.py` | Metrics queue `Queue3` / `metrics_queue_obj`. Publish only. |
| `src/db_services/metrics_service.py` | Builds the queue payloads: `build_history_and_metrics_payload`, `build_orchestrator_log_data`, `create_batch_conversation_logs`, `publish_plan_history_update`; writes the conversation cache (`save_conversations_to_redis`). |
| `src/db_services/conversationDbService.py` | Postgres reads of `conversation_logs`: `find_conversation_logs`, `find_completed_batch_conversations`, `find_rerun_logs`. Its three write functions have no callers. |
| `src/services/cache_service.py` | Redis client and helpers. Adds the `AIMIDDLEWARE_<ENV>_` prefix to every key. Locks. |
| `src/services/cache_utils.py` | Tag-based cache: `extract_cache_tags`, `store_in_cache_with_tags`. |
| `src/send_alert.py` | `send_alert`: internal alert to a platform webhook (`webhook_url`, else the default), or (unused path) the org's own alert webhooks. |
| `src/utils/alert_utils.py`, `src/utils/alert_template.py` | Alert payload builders and `send_internal_alert`. `DEFAULT_WEBHOOK_URL` is read from `src/configs/constant.py:alert_webhook`. |
| `src/db_services/webhook_alert_Dbservice.py` | `get_webhook_data`: the org's Mongo `alerts` docs. |
| `src/handler/executionHandler.py` | `handle_exceptions` decorator on `chat_multiple_agents`, `chat`, `embedding`, `batch`, `image`. |
| `src/services/grafana.py` | Pyroscope profiling setup, imported first thing in `index.py`. |
| `src/utils/formatter.py` | Rich-UI template filler `apply_variables_to_template_json` (used by the `is_template` branch in `src/services/utils/common_utils.py`). Not queue-related; it lives here by history. |
| `src/db_services/orchestrator_history_service.py` | `OrchestratorDataCollector`. No callers; dead code. |

## How it works

### 1. The three queues

| Queue | Name: env var, else fallback | Producer | Consumer |
|---|---|---|---|
| Main (requests) | `QUEUE_NAME`, else `AI-MIDDLEARE-DEFAULT-{ENVIRONMENT}` | this repo: `chat_completion` when the response goes out by webhook/RTLayer, `rerun_messages_route` via `queue_rerun_messages` | this repo, only on pods with `CONSUMER_STATUS=true`: `Queue.process_messages` → `image` or `chat_multiple_agents` |
| Log | `LOG_QUEUE_NAME`, else `AI-MIDDLEARE-DATA-QUEUE-{ENVIRONMENT}` | this repo (see section 3) | gtwy-node src/consumers/logQueueConsumer.js (prefetch 10) |
| Metrics | `METRICS_QUEUE_NAME`, else `AI-MIDDLEWARE-METRICS-QUEUE-{ENVIRONMENT}` | this repo | gtwy-node src/consumers/metricsQueueConsumer.js: buffers rows, bulk-inserts into Timescale raw_data every 100 messages or 5 minutes |

The `MIDDLEARE` typo in two fallbacks is real. Do not fix it: a renamed queue orphans whatever is queued under the old name.

Mechanics (`src/services/commonServices/queueService/baseQueue.py`):

1. `ConnectionManager` is a process singleton holding one `connect_robust(QUEUE_CONNECTIONURL)` connection and one channel per queue name. Each queue class is a singleton too, created at import time.
2. `create_queue_if_not_exists` declares the queue and `<name>-Failed`, both durable. `index.py:lifespan` calls it for the main and log queues on **every** pod. The metrics queue is never declared by Python.
3. `publish_message` JSON-encodes the dict, publishes on the default exchange with `routing_key=<queue name>`, persistent delivery, and header `retry_count` = attempt number. It tries 3 times with 1 s then 2 s backoff, then raises. After **each** failed attempt it awaits a RabbitMQ failure alert before the backoff (section 7), so a publish that fails all 3 tries sends 3 alerts and still raises.
4. `Queue.consume_messages` sets QoS `prefetch_count = PREFETCH_COUNT` (default 50) and consumes forever. Each message goes through `_message_handler_wrapper`:
   - `globals.is_ready` is false (SIGTERM received): `nack(requeue=True)` and return, so a draining pod takes no new work.
   - Otherwise, inside `message.process()` (ack on exit): decode JSON, call the callback. A `JSONDecodeError` or any exception publishes `{"error": ..., "original": ...}` to `<QUEUE_NAME>-Failed`. The original message is still acked.

### 2. What `CONSUMER_STATUS` changes

All pods serve HTTP and publish. `CONSUMER_STATUS` (compared with `.lower() == "true"` in `index.py:lifespan`) additionally:

1. starts the main-queue consumer task (`consume_messages_in_executor`);
2. starts the batch-results cron `src/services/utils/batch_script.py:repeat_function` (every 900 s, see [batch-api](batch-api.md));
3. tags Pyroscope profiles `service_type=consumer` instead of `producer` (`src/services/grafana.py`, case-sensitive `== 'true'`).

Queue declaration and every publisher run regardless of it.

### 3. History and metrics: publish, never write

```text
provider handler → historyParams + parsed_data["usage"]
  └─ process_background_tasks (src/services/utils/common_utils.py)
       ├─ build_history_and_metrics_payload(dataset, history_params, version_id)   (one per agent)
       │    or build_orchestrator_log_data(transfer_chain) when orchestrator_flag
       ├─ _update_history_redis  → conversation cache + nd_metrix_bridges_ (background task)
       ├─ make_request_data_and_publish_sub_queue → side-job blocks (memory, suggestions, billing…)
       ├─ sub_queue_obj.publish_message(log message)
       ├─ metrics_queue_obj.publish_message({"save_metrics": [...]})
       └─ apply_billing_events(data["billing"])
```

`historyParams` comes from `BaseService.prepare_history_params` ([tool-calling](tool-calling.md)); `usage` from the token calculator ([llm-providers](llm-providers.md)). Agent transfers collect each agent's entry in `TRANSFER_HISTORY` and the last agent publishes them all ([chat-orchestration](chat-orchestration.md)).

Every publisher, and the key it sends:

| Publisher | Log queue key | Metrics |
|---|---|---|
| `src/services/utils/common_utils.py:process_background_tasks` | full side-job message + `save_history` (list) or `save_orchestrator_history` | `save_metrics` |
| `src/services/utils/common_utils.py:_publish_history_to_queue` (error path, from `save_error_history`) | `save_history` | `save_metrics` |
| `src/services/commonServices/baseService/baseService.py` (error before a response) | `save_history` | none |
| `src/services/commonServices/reviewer_service_helpers.py` (each reviewer round) | `save_history`; full message on the final round | `save_metrics` |
| `src/db_services/metrics_service.py:create_batch_conversation_logs` (batch submit) | `save_batch_history` (one row per item, `batch_data.status = "queued"`) | none |
| `src/services/utils/batch_script.py:repeat_function` (batch results) | `update_batch_history` | `save_metrics` |
| `src/db_services/metrics_service.py:publish_plan_history_update` (todo mode) | `update_history` | none |

### 4. Reading history back

- `src/controllers/conversationController.py:getThread` → `find_conversation_logs`: the last 3 rows with `status` true for org + thread + sub-thread + bridge, oldest first, each expanded into `user`, `tools_call` (if any) and `assistant` messages. Called by `manage_threads` only on a Redis miss.
- Batch handlers call `find_completed_batch_conversations`, which also skips rows whose `batch_data->>'status'` is `queued`.
- `queue_rerun_messages` calls `find_rerun_logs` (by `message_ids`, or the latest 6 rows of a thread).
- Conversation cache: `cd_conversation_{version_id or bridge_id}_{thread_id}_{sub_thread_id}`, written by `save_conversations_to_redis` with TTL `CONVERSATION_CACHE_TTL` (2 hours). It keeps only `role`, `content` and `user_urls`, drops `tools_call`/`tool`/empty entries, and drops the oldest 2 once the list exceeds 9.

### 5. Redis helpers (`src/services/cache_service.py`)

Every helper prepends `REDIS_PREFIX` = `AIMIDDLEWARE_{ENVIRONMENT}_`. gtwy-node uses the same prefix, so both repos see the same keys on a shared Redis. The client has 10 s connect and read timeouts (`SERVICE_TIMEOUTS["redis"]`).

| Helper | Behaviour |
|---|---|
| `store_in_cache(key, data, ttl=DEFAULT_REDIS_TTL)` | `make_json_serializable` then `json.dumps`; default TTL 172800 s (2 days). Returns False on error. |
| `find_in_cache(key)` | Returns the **raw JSON string** (or None). Callers `json.loads` it. |
| `incr_in_cache`, `incrbyfloat_in_cache`, `get_float_in_cache` | Atomic counters on bare-number keys (spend counters, gpt-memory turn counter). |
| `expire_if_unset` | `EXPIRE NX`, with a TTL check fallback for Redis < 7. |
| `sadd_in_cache`, `smembers_in_cache` | Sets (apikey period refs). |
| `delete_in_cache(str or list)` | Prefixes and deletes. |
| `acquire_lock` / `release_lock` | `SET lock_{key} NX EX ttl` (default 1800 s). Used by the batch cron, usage alerts and Gtwy_Browser. |
| `find_in_cache_with_prefix` | `KEYS prefix*` then GET each. Used by the batch cron for `nd_batch_` keys. |
| `clear_cache(request)` | Deletes ids, or **every** prefixed key. No route uses it. |

Key naming lives in `src/configs/constant.py:redis_keys` (gtwy-node mirrors it in its src/configs/constant.js):

- `cd_` = can delete: a cache of data that lives in a DB and is rebuilt on a miss (agent config, conversation, last transferred agent, direct agent).
- `nd_` = no delete: Redis is the only store, or it is a cost/usage accumulator (spend counters, rate limits, batch ids, plans, gpt memory, billing holds, browser registry).
- gtwy-node's bulk "clear Redis" (clearRedisCache in its src/controllers/utils.controller.js) deletes every prefixed key **except** those starting with `nd_` (and `blacklist:` keys). Lock keys (`lock_…`), tag sets (`tag:…`) and anything else without `nd_` are wiped by it.

### 6. Tag-based invalidation (`src/services/cache_utils.py`)

The agent config blob (`cd_bridge_data_with_tools_…`, written by `src/db_services/ConfigurationServices.py:get_bridges_with_tools_and_apikeys`) is stored with `store_in_cache_with_tags`:

1. `extract_cache_tags` reads the blob and returns sorted tags from `src/configs/constant.py:tag_keys`: `tag:agent:{parent_id or _id}` (plus `…_env_{environment}`), `tag:version:{_id}` when it is a version, `tag:tool:` for `apiCalls` keys, `pre_tools_data[]._id` and `post_tool`, `tag:apikey:` for `apikey_object_id` values, `tag:folder:`, `tag:connected_agent:` for `connected_agent_details` and `agent_name_info` keys, `tag:wrapper:`, `tag:rag:` for `doc_ids[].resource_id`.
2. One `MULTI` pipeline: `SET` the blob with its TTL, then for each tag `SADD {prefix}{tag} {prefix}{blob key}` and `EXPIRE tag ttl+600` so the index outlives the blob.
3. Python never purges by tag. gtwy-node's Mongoose cacheInvalidationPlugin calls invalidateByTag(type, _id) after saves/updates/deletes on Configuration (agent, connected_agent), BridgeVersion (version), ApiCall (tool), ApikeyCredentials (apikey), Folder, PromptWrapper and RagCollection. It `SMEMBERS` the tag set, deletes every member and then the set.

### 7. Alerts

`send_alert(..., is_external_error=False)` is the only form used in this repo (callers: `handle_exceptions`, `src/services/commonServices/common.py` for missing variables and fallback retries, `src/services/commonServices/api_executor.py` for a failed provider call when `alert_on_retry` is set, `process_background_tasks_for_error`, and `BaseQueue.publish_message` for a failed publish). It:

1. builds `build_base_payload`: the `error_log` dict spread at the top level, plus `agent_id`, `org_id`, `bridge_name`, `org_name`, `alert_type`, `api_name`, `service`;
2. `send_internal_alert` adds `error_location` and `ENVIRONMENT`, then POSTs to `webhook_url` if the caller passed one, else `DEFAULT_WEBHOOK_URL`, with `src/services/utils/apiservice.py:fetch`. Both URLs are viaSocket flows in `src/configs/constant.py:alert_webhook`: `default_webhook_url` for everything, `rabbit_mq_failure_url` passed only by `publish_message`.

Alert types are `src/configs/constant.py:alert_types`. The publish-failure alert is the exception: its `alert_type` is `rabbitmq_publish_failure_<queue name>`, which is not in `alert_types` (fine on the internal path, which never looks the type up). Request-path callers run it in the background (`asyncio.create_task` or `asyncio.gather`) and skip it when `is_playground`. `publish_message` instead awaits it inline, playground or not, with no agent or org fields set. `send_alert` catches and logs its own errors, so a failed alert never raises. A failed `chat` alerts twice: once from `process_background_tasks_for_error` (type `Error`, via `save_error_history`) and once from `handle_exceptions` when `chat` re-raises.

Publish-failure alert body (`error_log` from `publish_message`, merged by `build_base_payload`):

```json
{ "type": "rabbitmq_publish_failure_<queue>", "token": null, "reason": "<traceback.format_exc(), else str(e)>",
  "environment": "<ENVIRONMENT>", "alert_type": "rabbitmq_publish_failure_<queue>", "agent_id": null, "org_id": null,
  "bridge_name": null, "org_name": null, "api_name": null, "service": null, "ENVIRONMENT": "<ENVIRONMENT>" }
```

`publish_message` imports `send_alert` inside its `except`, not at the top of `src/services/commonServices/queueService/baseQueue.py`. A top-level import is circular (`send_alert` → `alert_utils` → `baseService` → `queueLogService` → `baseQueue`); 4f6d92ec (#1790, "fix loop import") moved it inside. Keep it lazy.

The `is_external_error=True` branch (load the org's `alerts` docs, add a default entry, filter by `alertType` and `bridges`, send each through `sendResponse`) has no caller here. Customer-facing alert webhooks run in gtwy-node from the log queue's `broadcast_response_webhook` block. A second internal channel, `src/services/commonServices/baseService/utils.py:unknown_error_handler_alert`, is used for gateway (502/520) errors in `fetch`. It posts to the same flow as `rabbit_mq_failure_url`, but its URL is hard-coded there, not read from `alert_webhook`: changing one does not move the other.

### 8. `handle_exceptions` and timeouts (`src/handler/executionHandler.py`)

The decorator catches everything and returns `JSONResponse(400)`:

1. A timeout type (`ServerSelectionTimeoutError`, `NetworkTimeout`, `ExecutionTimeout`, Redis `TimeoutError`, `APITimeoutError`, `asyncio.TimeoutError`, `TimeoutError`) → `{"error": TIMEOUT_ERROR_MESSAGE}`.
2. `ValueError(dict)` → that dict is the body. This is how `chat` returns `{"success": false, "error": ..., "message_id": ...}`.
3. A string that parses as JSON → the parsed value; any other string → `{"error_message": "..."}`.
4. Unless `body.is_playground`, it fires an internal `send_alert` with `error_location` (file, function, code line) from the last traceback frame.

> [!WARNING]
> The timeout branch rarely fires. All decorated functions catch their own errors inside a `try` and re-raise them as `ValueError` (`chat` and `image` with an error dict, `embedding` and `batch` with the original exception as the argument), and `chat_multiple_agents` returns its own 500. So a Mongo or Redis timeout arrives wrapped, and the caller gets the wrapped text, not `TIMEOUT_ERROR_MESSAGE`. To get the friendly message there, the timeout type must survive the re-raise: `embedding` and `batch` keep it as `__cause__`, but `chat` raises `from None` and keeps only text. On the queue path the returned JSONResponse is thrown away; the user hears about the error only through what `chat` already sent to their `response_format`.

Timeouts come from `src/services/utils/time.py`: `with_timeout(coro, service)` wraps Mongo calls in `asyncio.wait_for` (`SERVICE_TIMEOUTS`: mongo 60 s, pre_function 60 s), and the Redis client has socket timeouts. `log_slow_call` prints `[SLOW]` lines above `SLOW_CALL_THRESHOLDS` (Redis 300 ms, Mongo/PG 500 ms).

## Contracts

Log queue message from `process_background_tasks` (blocks built in `src/services/commonServices/baseService/utils.py:make_request_data_and_publish_sub_queue`). gtwy-node acts on each key:

```jsonc
{
  "save_history": [conversation_log_data, ...],      // INSERT conversation_logs (+ sub-thread name)
  "save_orchestrator_history": { ..., "sub_thread_data": {...} }, // INSERT orchestrator_conversation_logs
  "billing": [ { "model", "service", "bridge_id", "thread_id", "user_id", "folder_id", "is_embed", ...event } ] | null,
  "background_billing": { "org_id", "message_id", "user_id", "folder_id", "is_embed" } | null, // null = own API key, no charge
  "type": "chat" | "image" | ...,                     // "image" stops after history + billing
  "save_agent_memory": { "user_message", "assistant_message", "bridge_id", "cache_on", "is_cache_hit", "resource_id", ... },
  "validateResponse": { "alert_flag", "configration", "bridgeId", "message_id", "org_id" },
  "save_files_to_redis": { "thread_id", "sub_thread_id", "bridge_id", "files" },
  "check_handle_gpt_memory": { "gpt_memory": bool, "type" }, "handle_gpt_memory": { "id", "user", "assistant", "pending_turns", ... },
  "check_chatbot_suggestions": { "bridgeType" }, "chatbot_suggestions": { ... },
  "broadcast_response_webhook": { "bridge_id", "org_id", "response", "error_type": "broadcast_response", ... },
  "metrics_service": { "dataset", "history_params", "version_id" }   // ignored by the Node consumer
}
```

Other log messages carry one key: `{"update_history": {"message_id", ...columns}}`, `{"save_batch_history": [...]}`, `{"update_batch_history": [{"batch_id", "message_id", "update_data"}]}`.

`conversation_log_data` (from `build_history_and_metrics_payload`; `thread_flag` and `response_format` are added by `_attach_sub_thread_extras`):

```text
llm_message, reasoning, user, chatbot_message, updated_llm_message=None, error, user_feedback=0,
tools_call_data, annotations, message_id (str), thread_id, sub_thread_id, version_id, bridge_id,
user_urls, llm_urls, AiConfig, fallback_model, org_id, service, model, status (= usage.success),
tokens {input_tokens, output_tokens, total_tokens, …token_usage, expected_cost, cost, credits},
variables, latency (dict), firstAttemptError, finish_reason, parent_id, child_id, prompt,
is_cached, plans, testcase_id, testcase_data
```

Metrics row (one per `dataset` entry that has `orgId`): `org_id, bridge_id, version_id, thread_id, model, input_tokens, output_tokens, total_tokens, apikey_id, latency (over_all_time), success, cost (expectedCost), time_zone ("Asia/Kolkata"), service`.

Other invariants:

- Queue names must be set identically in both repos. gtwy-node reads `LOG_QUEUE_NAME` and `METRICS_QUEUE_NAME` with no fallback.
- Wrap every payload in `make_json_serializable` before `publish_message`. A raw `datetime` or `ObjectId` makes `json.dumps` raise inside the retry loop, so the publish fails 3 times and sends 3 RabbitMQ failure alerts.
- gtwy-node maps columns explicitly (saveConversationHistory field list, and UPDATABLE_COLS for `update_history`). A new key in `conversation_log_data` is silently dropped until gtwy-node maps it.

## Gotchas

> [!WARNING]
> The metrics queue is never declared by Python. Publishing to an undeclared queue through the default exchange is unroutable: aio-pika publishes with `mandatory=True` but the channel does not raise on returns, so `publish_message` logs success and the metrics are gone. The queue exists only once gtwy-node's consumer has asserted it. On a fresh broker or a new `METRICS_QUEUE_NAME`, start gtwy-node first.

> [!WARNING]
> `CONSUMER_STATUS` must be set. `index.py:lifespan` calls `Config.CONSUMER_STATUS.lower()`, so an unset value crashes startup. `TRUE` (as in `.env.example`) starts the consumer, but Pyroscope tags the pod as `producer` because `src/services/grafana.py` compares case-sensitively.

> [!WARNING]
> On a shared broker, every process with `CONSUMER_STATUS=true` and the same `QUEUE_NAME` competes for the same messages, and every gtwy-node with the same log/metrics names saves your history. Use your own three queue names for local testing.

> [!WARNING]
> A failing publish is slow. The RabbitMQ failure alert is awaited inside the retry loop on every failed attempt, and `fetch` allows 600 s per POST and retries timeouts, connection errors and 502/520 up to 3 times. So a RabbitMQ outage costs 3 alert POSTs plus 3 s of backoff before `publish_message` raises, and a slow or unreachable alert flow stretches that to minutes. The main-queue publish in `chat_completion` and the log/metrics publish in `process_background_tasks` both run before the HTTP reply. An attempt that fails and then succeeds on retry still sends one alert.

> [!WARNING]
> `-Failed` queues are write-only: nothing consumes them. And they rarely get anything, because `chat_multiple_agents` and `image` are wrapped by `handle_exceptions` (and `chat_multiple_agents` catches everything itself), so errors come back as a JSONResponse, which `process_messages` discards. Only undecodable messages reach `<QUEUE_NAME>-Failed`. gtwy-node nacks failed log messages without requeue (dropped) and moves failed metrics batches to `<METRICS_QUEUE_NAME>-failed` (lower-case `f`).

> [!WARNING]
> After SIGTERM, `_message_handler_wrapper` nacks with requeue while the consumer is still attached. RabbitMQ can redeliver the same messages to the same pod in a tight loop until it exits or the broker gives them to another consumer.

> [!WARNING]
> `clear_cache` in `src/services/cache_service.py` deletes every `AIMIDDLEWARE_<ENV>_` key, including `nd_` spend counters, rate limits, batch ids and billing holds. It is not routed today. Do not wire it up as is; gtwy-node's clear endpoint skips `nd_` keys.

> [!WARNING]
> `find_in_cache` returns JSON text, not the value. A string stored with `store_in_cache` comes back with quotes (`"\"abc\""`); `chat_multiple_agents` strips them with `json.loads`. `find_in_cache_with_prefix` uses `KEYS`, which blocks Redis on a large keyspace.

> [!WARNING]
> The Postgres reads in `src/db_services/conversationDbService.py` are synchronous SQLAlchemy calls inside `async def`. A slow query blocks the event loop of the whole pod.

> [!TODO] Needs a human: for a non-orchestrator agent transfer, `process_background_tasks` sends one `save_history` entry per agent in the chain, but gtwy-node's saveConversationHistory inserts only `historyEntries[0]` (the first agent). The other agents' rows are never saved, while all their metrics are. Is that intended, or should gtwy-node insert every entry?

> [!TODO] Needs a human: `_update_history_redis` keeps a running token total in `nd_metrix_bridges_{bridge_id}` with a non-atomic read-add-write, and nothing in either repo reads it. Can it be removed?

## Why it is built this way

- **Python publishes, Node writes.** Direct Postgres writes (savehistory_consolidated) were removed in 52a13765 (Mar 2026), and history rows have gone to gtwy-node over the log queue since. Metrics got their own queue in c425fd7b (Apr 2026) so gtwy-node can batch Timescale inserts (100 rows or 5 minutes) instead of one insert per call. The leftover write functions in `src/db_services/conversationDbService.py` (`storeSystemPrompt`, `update_conversation_log`, `add_bulk_user_entries`) have no callers.
- **Conversation cache seeds the first turn.** `_update_history_redis` writes the cache even when the thread had no history. Before, a new thread cached nothing and its second turn read Postgres, which only the Node worker fills; without that worker the thread lost its memory (comment in `_update_history_redis`). 934540c1 (Sep 2026) also cut the cache to role/content/urls and set the 2-hour TTL.
- **Readiness guard on the consumer** (7fb23bb0): SIGTERM sets `is_ready = False`, so a draining pod returns messages to the queue instead of starting work it may not finish.
- **Per-service timeouts** (3a4e05e2, #1631): a hung Mongo or Redis call used to hold the request forever. Now Mongo calls time out at 60 s, Redis sockets at 10 s, and the user gets a friendly 400.
- **`AIMIDDLEWARE_<ENV>_` prefix** (b5318a06, #1422): environments share one Redis without clobbering each other.
- **`cd_` / `nd_`** (6008933f, #1579): lets an operator flush caches without wiping spend counters, rate limits and in-flight batch/billing state.
- **Tag purge** (4788919c, #1540): the agent blob is cached for 2 days. Deleting it when any joined entity changes is cheaper than a short TTL.
- **Lock TTL 1800 s** (a0dcdee2, #1527): longer than the 900 s batch poll plus processing, so two pods never process the same batch.
- **Atomic spend counters** (`incrbyfloat_in_cache` docstring): two requests ending together lost an increment with read-parse-add-write.
- **Alert refactor** (16eb360b, #1400): replaced `send_error_webhook.py` with `send_alert` + templates. <!-- verify:ignore -->
- **RabbitMQ failure alert** (1ec125c0, #1669): a failed publish used to show up only in logs. It now posts to its own flow (`rabbit_mq_failure_url`), which is why `send_alert` gained `webhook_url` and the URLs moved into `alert_webhook`.

> [!TODO] Needs a human: `publish_message` alerts on every failed attempt, not once after the last one, so a publish that recovers on retry still alerts and an outage sends 3 alerts per message. Is per-attempt alerting intended?

> [!TODO] Needs a human: why did history writes move from Python to gtwy-node (52a13765)? The commit only says "remove consolidated conversation log functionality".

## Changing it safely

- **New field on the conversation-log row.** Add it in `build_history_and_metrics_payload` (and `build_orchestrator_log_data` if orchestrator rows need it), then map it in gtwy-node: the saveConversationHistory field list, UPDATABLE_COLS if `update_history` should set it, and the Postgres model + migration ([data-model](../data-model.md)).
- **New side job after a response.** Add a block in `make_request_data_and_publish_sub_queue` and a handler in gtwy-node's logQueueConsumer.js. Without the Node handler the key is ignored.
- **New queue.** Subclass `BaseQueue` as a singleton, read its name from a new `Config` var (document it in [environment](../environment.md)), and call `connect()` + `create_queue_if_not_exists()` in `index.py:lifespan` so it is declared before the first publish.
- **New Redis key.** Add it to `src/configs/constant.py:redis_keys` with `cd_` (rebuildable) or `nd_` (must survive a flush), mirror it in gtwy-node's constant.js if Node touches it, and go through the `cache_service` helpers so the env prefix is applied. Never use the raw `client` without `REDIS_PREFIX`.
- **New cached entity.** Emit its tag in `extract_cache_tags`, add it to `tag_keys` here and to gtwy-node's tagKeys.js, and attach cacheInvalidationPlugin to its Mongoose model there.
- **New alert destination.** Add the flow URL to `alert_webhook` and pass it as `send_alert(..., webhook_url=...)`. Leave `webhook_url` out to use `default_webhook_url`.
- **New alert type.** Add it to `alert_types`, a template in `src/utils/alert_template.py`, a branch in `get_details_payload`, and the enum in gtwy-node's Alerting model and its Joi validation.
- **Check it worked.** Run one queued request (webhook `response_format`) with `CONSUMER_STATUS=true` and gtwy-node consumers on your own queue names. Look for "Message published to …" logs, then the new `conversation_logs` row and Timescale raw_data rows. See [chat-completion flow](../flows/chat-completion.md) and [startup and background jobs](../flows/startup-and-background-jobs.md).
