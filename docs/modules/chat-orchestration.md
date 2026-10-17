---
covers: ["src/services/commonServices/common.py", "src/services/utils/common_utils.py", "src/services/commonServices/createConversations.py", "src/services/commonServices/streaming_service.py", "src/services/commonServices/response_caching_*.py", "src/services/utils/transfer_handler.py", "src/services/utils/maximum_iterations_utils.py", "src/services/utils/gpt_memory.py", "src/services/utils/openai_sdk_utils.py", "src/controllers/conversationController.py", "src/routes/v2/modelRouter.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Chat orchestration

The code that turns an already-configured request (agent config merged into the body by the middleware) into one completion: it picks the agent, builds the prompt, runs pre-tools, history and memory, calls the provider handler, falls back, streams, transfers to other agents, and publishes history, metrics and billing after the response. It is called by the `/api/v2/model` routes, by the RabbitMQ consumer for queued requests, by todo mode, by testcase runs, and recursively by agent-as-tool calls.

The full request path is in [chat-completion flow](../flows/chat-completion.md). This page is about how this module works and where it breaks.

| File | Role |
|---|---|
| `src/routes/v2/modelRouter.py` | Routes `/chat/completion`, `/ai/completion`, `/openai/responses`, `/batch/chat/completion`, `/testcases`, `/rerun`. Decides queue vs direct, and chat vs embedding vs image. |
| `src/services/commonServices/common.py` | `chat_multiple_agents` (pick agent), `chat` (the pipeline), `embedding`, `batch`, `image`. All wrapped in `handle_exceptions`. |
| `src/services/utils/common_utils.py` | Every step `chat` calls: `parse_request_body`, `handle_pre_tools`, `manage_threads`, `prepare_prompt`, `build_service_params`, `normalize_response_type`, `sse_stream_and_finalize`, `process_background_tasks`, error history. 2,100+ lines, the most-changed file in the repo. |
| `src/services/commonServices/streaming_service.py` | `StreamingService`: SSE event emitter backed by an `asyncio.Queue` (or RTLayer mode). |
| `src/services/commonServices/response_caching_service.py` | `handle_response_caching`: semantic answer cache in front of `class_obj.execute()`. |
| `src/services/commonServices/response_caching_utils.py` | Hippocampus vector search + Mongo `agent_memories` lookup for the cache. |
| `src/services/utils/transfer_handler.py` | `check_transfer_from_codes_mapping`: spots an agent-transfer tool call (used by BaseService). |
| `src/services/utils/maximum_iterations_utils.py` | Process-local tool-call budget per agent per message, with an owner token. |
| `src/services/utils/gpt_memory.py` | Reads long-term thread memory (Redis, then a viaSocket flow). |
| `src/services/commonServices/createConversations.py` | `ConversationService`: turns stored history + memory into each provider's message format. |
| `src/controllers/conversationController.py` | `getThread`: history from Postgres when the Redis cache misses. |
| `src/services/utils/openai_sdk_utils.py` | Adapter for `/openai/responses`: rewrites an OpenAI Responses request into an internal body and formats the reply back. |

## How it works

### 1. Route: queue or run now (`chat_completion`)

1. Auth and rate limits run as a dependency: 100 per `body.bridge_id`, 20 per `body.thread_id` (`auth_and_rate_limit`). `/ai/completion` limits per org (plus per thread) and injects the org's hidden agent id first ([agent-configuration](agent-configuration.md)).
2. `add_configuration_data_to_body` has already merged the agent config into the body and set `bridge_configurations` and `credit_hold_token` ([agent-configuration](agent-configuration.md), [billing](billing-and-limits.md)).
3. A new `message_id` (uuid1) is stamped. `configuration.response_format` is moved to `settings.response_format`.
4. Playground requests whose agent is not set to stream get `settings.response_format` forced to RTLayer on channel `{org_id}_{bridge_id}[_{version_id}]_{user_id}`.
5. If `settings.response_format.type` is anything but `default` (and it is not a playground webhook), the request is stamped with `created_at`, published to the main queue and the route returns at once. If publishing fails, the credit hold is released here. `BaseQueue.publish_message` tries 3 times (1 s, then 2 s backoff) and awaits a failure alert POST after each failed attempt, so during a RabbitMQ outage this reply is slow and sends up to 3 alerts ([queues-history-metrics](queues-history-metrics.md)).
6. Otherwise `configuration.type` picks `embedding`, `image` or `chat_multiple_agents`.

The queue consumer (`src/services/commonServices/queueService/queueService.py`) later calls the same `chat_multiple_agents` / `image` with the same body.

### 2. Pick the agent (`chat_multiple_agents`)

- `bridge_configurations` holds the primary agent and every connected agent, keyed by id.
- Sticky agent: if this thread was transferred before, Redis key `last_transffered_agent_` + `{primary_bridge_id}_{thread_id}_{sub_thread_id}` names the agent that answered last, and that agent becomes primary.
- The chosen agent's config is merged over the body (`primary_body.update(primary_config)`); only `wrapper_id` and `settings.response_format` from the request survive the merge.

### 3. The pipeline (`chat`)

In order, all mutating one `parsed_data` dict:

1. `parse_request_body` flattens body + state into `parsed_data`. The credit hold token is popped from the body; this frame now owns releasing it.
2. MCP tools are prefetched when the model uses client-side MCP ([tool-calling](tool-calling.md)). Reviewer settings become `_reviewer_bridge_id` / `_reviewer_prompt` / `_reviewer_tools` ([reviewer](reviewer-and-guardrails.md)).
3. `init_tool_count` claims the tool-call budget for `{bridge_id}-{message_id}` (`maximum_iterations`, default 3, at least 25 when `Gtwy_Browser` is enabled, see `_resolve_maximum_iterations`).
4. `process_variable_state` fills variable defaults; `setup_agent_tools` resolves the pre-tool's args (a built-in pre-tool gets its static `config` such as `prompt`, `resource_id`/`collection_id` or `url` as args, overridden by args mapped to agent variables; names in `config.required` and `config.params` are also filled from same-named variables); `apply_prompt_wrapper` wraps the prompt; `add_default_template` appends the current-time block; `_user_message` is added to variables.
5. Optional auto model selection, then `load_model_configuration` builds `custom_config` from the model's schema in `model_config_document` (only keys the model declares, plus `response_type`).
6. `handle_pre_tools` runs the pre-tool (`custom_function` as a POST to the tool's stored `url`, `query_refiner`, `rag_knowledgebase`, `gtwy_web_search`) and writes its result into a variable (`pre_function`, `rag_pre_result`, `web_search_pre_result`).
7. `manage_threads` loads history: Redis `conversation_` key first, else `getThread` (last 3 successful rows from Postgres). No `thread_id` means a new uuid1 thread and memory off.
8. `prepare_prompt` loads GPT memory (if on), replaces variable placeholders in the prompt, applies the agent template and the chatbot-suggestions template, and sets `response_type`.
9. `configure_custom_settings` maps generic keys to the provider's keys; `build_service_params` builds the handler params (with a fresh `TokenCalculator`); `validate_json_schema_configuration` and `normalize_response_type` fix up `response_type` against the types the model lists (`model_supports_response_type`):
   - `json_schema` the model cannot enforce: the schema is inlined into the prompt and `response_type` becomes `{"type": "json_object"}`, or is dropped if the model does not list `json_object` either.
   - `json_object`: sent bare as `{"type": "json_object"}`; a `json_schema` stored next to it goes into the prompt. If the model does not list `json_object`, it is dropped and a "reply with only JSON" line goes into the prompt.
   - `text`: its `text` value goes into the prompt; dropped if the model does not list `text`.

   The prompt lines are stored as `_json_schema_instruction` / `_text_instruction` and `chat` appends them to `configuration.prompt`.
10. Plan mode: with an `action` it hands off to `handle_todo_mode` and returns; without one the planner prompt is injected and the call continues ([todo-mode](todo-mode.md)).
11. `Helper.create_service_handler` builds the provider handler ([llm-providers](llm-providers.md)). If it is in stream mode, see Streaming below.
12. `handle_response_caching` → `class_obj.execute()` (the provider call and tool loop, [tool-calling](tool-calling.md)). JSON output is checked and repaired with `repair_json`.
13. Transfer, fallback (below), then: cost, rich-UI template render, usage metrics, reviewer loop, post-tool, `sendResponse` to webhook/RTLayer, testcase scoring, `process_background_tasks`, sticky-agent cache write (3 days).
14. `finally`: clean the tool budget, update cost / last-used / API-key status in the background, release the credit hold.

`batch` and `image` reuse a subset of these steps. `embedding` only loads the model config and calls the embedding handler; it writes no history.

### Streaming (SSE)

When `customConfig.stream is True`, BaseService creates a `StreamingService(mode="sse")`. `chat` then:

1. Starts `sse_stream_and_finalize` as a background task and returns `StreamingResponse(streamer.generator())` straight away.
2. Defers tool-budget cleanup (`cleanup_tool_count_after`) and credit release (`release_credits_after`) until that task ends.
3. `sse_stream_and_finalize` repeats the non-streaming tail: cache/execute, JSON repair, fallback (emits `fallback`), transfer, template (`template_response`), usage, reviewer, post-tool, history, then `done` and `close()`.

With a post-tool configured, model deltas are sent as `reasoning` events (`redirect_delta_to_reasoning`) and only the post-tool output is sent as `delta`.

### Agent transfer

BaseService detects a tool call whose args have `action_type == "transfer"` (`check_transfer_from_codes_mapping`) and returns `transfer_agent_config` instead of running tools. The current agent's history row is appended to `TRANSFER_HISTORY[transfer_request_id]`, then `chat` is called again for the target agent with `user = _query`, `parent_bridge_id`, the same `transfer_request_id` and the same `bridge_configurations`. The last agent in the chain saves every row in one go (`process_background_tasks`), linking them by `parent_id` / `child_id`, or as one orchestrator row when `orchestrator_flag` is set.

### Fallback

If the first attempt raises or returns `success: False`, and `settings.fall_back.is_enable` is on, and `fallback_allowed_on_plan` allows it, `apply_wallet_fallback` swaps the credential and wallet flag, the model config is rebuilt from scratch, and the call runs once more. Success marks `response.data.fallback = true` and `firstAttemptError`. If both fail, the error says both.

Non-stream only (`chat`): a successful answer with `response.data.finish_reason == "truncated"` (a provider token-limit stop, mapped in `src/services/utils/formatters/finish_reason.py`) also gets the fallback retry. Before retrying, `chat` costs the primary call and, on wallet runs, parks it in `_wallet_primary_cost` so both calls are billed ([billing](billing-and-limits.md)). If the fallback then fails, `chat` restores the primary result, handler, key and wallet flag and returns the cut-off answer instead of an error. Both outcomes send the `retry_mechanism` alert, because `original_error` is set to "Response truncated by token limit on …".

### Response cache and memory

- Cache: only when `cache_on`, type `chat`, and a user message exist. Hippocampus search (top 1, `min_score=0.85`) on the user message scoped to the agent → `agent_memories.original_answer` in Mongo. A hit returns a formatted response with zero usage and `is_cached: true`, and skips the LLM.
- GPT memory: key `{thread_id}_{sub_thread_id}_{version_id or bridge_id}` in Redis, else fetched from a viaSocket flow and cached. Each provider builder in `ConversationService` injects it before the history (as a user/assistant exchange for most providers). It is regenerated by Node every 3rd turn (`GPT_MEMORY_TURNS_PER_CYCLE`), triggered from the log-queue payload.

### After the response (`process_background_tasks`)

Nothing is written to Postgres from Python. It builds history + metrics payloads and publishes them: one message to the log queue (`sub_queue_obj`, consumed by gtwy-node's log-queue consumer) and one to the metrics queue. It also seeds the Redis conversation cache, bumps the per-agent token counter, pushes each history row to RTLayer channel `{org_id}_{bridge_id}`, saves the plan in plan mode, and applies billing events. Errors go through `save_error_history` → one error row + alert. See [queues-history-metrics](queues-history-metrics.md).

## Contracts

Input to `chat_multiple_agents` / `chat` / `image` (built by `make_request_data` or by the caller):

```json
{
  "body": {
    "bridge_id": "…", "user": "hi", "thread_id": "…", "sub_thread_id": "…", "message_id": "uuid1",
    "configuration": {"model": "…", "type": "chat", "prompt": "…", "stream": false, "response_type": {…}},
    "settings": {"response_format": {"type": "default"}, "fall_back": {"is_enable": true, "service": "…", "model": "…"}, "maximum_iterations": 3},
    "service": "openai", "apikey": "<decrypted>", "variables": {}, "version_id": "…",
    "bridge_configurations": {"<agent_id>": {"configuration": {…}, "pre_tools_data": [], "post_tool_data": {}, "settings": {…}, "version_id": "…"}},
    "credit_hold_token": "optional, popped by chat()", "wallet": false, "created_at": "ISO, stamped if absent"
  },
  "state": {"profile": {"org": {"id": "…"}, "user": {"id": "…"}}, "timer": [], "version": 2},
  "path_params": {}
}
```

Private body flags (set only by this code and `call_gtwy_agent`, never by clients):

| Flag | Meaning |
|---|---|
| `_injected_streamer` | Reuse the caller's `StreamingService` (streamed transfer or streamed agent-as-tool). |
| `_nested_stream_call` | Child frame: do not `close()` the shared stream, emit `done`, emit `error` or call `sendResponse`. A failure is saved as an error row and returned as `{"success": false, "message": …}`, which the caller sees as the agent's tool result. |
| `_sync_injected_stream_call` | Await `sse_stream_and_finalize` inline instead of in a background task. |
| `_nested_agent_call` | Child of another agent in the same request (billing: hit fee once). |
| `primary_bridge_id` | Set by `chat_multiple_agents`; key for the sticky-agent cache. |

What `class_obj.execute()` must return (BaseService):

```json
{"success": true, "response": {"data": {"content": "…", "id": "…"}, "usage": {…}},
 "modelResponse": {…}, "historyParams": {"message": "…", "tools_call_data": []},
 "transfer_agent_config": {"agent_id": "…", "user_query": "…", "action_type": "transfer"},  // optional
 "stream_finish_reason": "tool_transfer", "error": "…"}                                  // optional
```

HTTP results:

| Case | Status | Body |
|---|---|---|
| Sync success | 200 | `{"success": true, "response": {"data": {..., "message_id", "fallback"?, "finish_reason"?}, "usage": {..., "cost"}}}` |
| Queued (webhook / RTLayer / playground non-stream) | 200 | `{"success": true, "message_id", "message": "Your response will be sent through configured means."}` |
| Error inside `chat` | 400 | `{"success": false, "error": "… For more support contact us at support@gtwy.ai", "message_id"}` (from `handle_exceptions`) |
| Error before `chat` (no configs) | 500 | `{"success": false, "error": …}` |
| Stream | 200 | `text/event-stream`, errors arrive as an `error` event |

SSE wire format is `data: {json}\n\n` with the event name inside the JSON (`"event": "start" | "delta" | "reasoning" | "tool_call" | "tool_result" | "fallback" | "template_response" | "review_phase" | "planning" | "browser_handoff" | "error" | "done"`, plus `task_*` events in plan mode). `done` carries `usage`, `message_id`, `finish_reason`, and the full `response` unless a rich-UI template was rendered (then the content went out in `template_response`).

Redis keys this module reads or writes (all prefixed `AIMIDDLEWARE_{ENVIRONMENT}_` by `cache_service`; names in [data-model](../data-model.md)):

| Key | TTL | Read / write |
|---|---|---|
| `cd_last_transffered_agent_{primary}_{thread}_{sub}` | 3 days | read `chat_multiple_agents`, write `chat` |
| `cd_conversation_{version_id or bridge_id}_{thread}_{sub}` | 2 h, max ~9 messages | read `manage_threads`, write `_update_history_redis` |
| `nd_metrix_bridges_{bridge_id}` | default 2 days | running token total, `_update_history_redis` |
| `{thread}_{sub}_{version_id or bridge_id}` | default | GPT memory, `get_gpt_memory` |

## Gotchas

> [!WARNING]
> **Streamed agent transfer ends with a bare `raise`.** In `sse_stream_and_finalize`, after a successful non-nested transfer (`src/services/utils/common_utils.py:2050`) there is a `raise` with no active exception. It raises `RuntimeError`, the outer `except` emits an `error` event, closes the shared stream and saves an error row, while the target agent is still streaming into the closed queue. Commit 52e28a34 replaced the original `return  # Agent B owns emit_done + close from here` with this `raise`. Test streamed transfers before touching this block.

> [!WARNING]
> **`TRANSFER_HISTORY` grows forever.** `chat` and `image` create `TRANSFER_HISTORY[transfer_request_id] = []` for every request, but the entry is deleted only when a transfer chain is saved. Every non-transfer request and every failed request leaves one entry in this process-global dict (`globals.py`).

> [!WARNING]
> **Errors are 400s, not exceptions.** `handle_exceptions` turns anything `chat` raises into `JSONResponse(400)` and sends an alert. Internal callers of `chat` / `chat_multiple_agents` (testcases, `call_gtwy_agent`, todo mode) get a `JSONResponse` back, not an exception; check `status_code`. A failed `chat` also alerts twice: once from `save_error_history`, once from `handle_exceptions`.

> [!WARNING]
> **`settings.response_format` must be a dict with `type`.** `chat`'s error path, `sendResponse` and history code index into it without a guard. A missing value turns the real error into a `TypeError`. The middleware defaults it for direct calls; any new caller must set `{"type": "default"}`.

> [!WARNING]
> **The credit hold is released exactly once, by its owner.** `chat` pops `credit_hold_token` from the body; streaming moves ownership to `release_credits_after`; the route releases it only if the queue publish failed. Do not read the token from the body after `chat` starts, and do not add another release path without the token: `release_credits` is idempotent per token, not per request.

> [!WARNING]
> **Fallback lives in two places.** `chat` (non-stream) and `sse_stream_and_finalize` (stream) each implement it. Both must call `fallback_allowed_on_plan` and `apply_wallet_fallback`. The streaming copy once skipped billing checks and let wallet runs fall back onto any model on the original key. They still differ: only `chat` retries a truncated answer, and only `chat` runs `normalize_response_type` for the fallback model.

> [!WARNING]
> **The agent merge clobbers request config.** `chat_multiple_agents` and `handle_agent_transfer` do `body.update(agent_config)`, so `body.configuration` is replaced by the stored agent config. A per-request override must be put into `bridge_configurations[agent_id]` before the call (as `getConfiguration` and `src/services/todo/executor_service.py` do).

> [!WARNING]
> **Playground non-stream requests do not answer over HTTP.** The route forces RTLayer for them, so they are queued and the HTTP reply is only an acknowledgement.

> [!WARNING]
> **The tool budget is in process memory.** `maximum_iterations_utils` keeps counters in a module dict keyed `{bridge_id}-{message_id}`. Only the frame holding the owner token may clean it, and for streams the cleanup waits for the stream task. Reusing a `message_id` for a different run of the same agent in one process shares the budget.

Smaller traps:

- `add_files_to_parse_data` reads Redis key `{bridge_id}_{thread_id}_{sub_thread_id}`, but gtwy-node's log-queue file saver writes `nd_files_{bridge_id}_{thread_id}_{sub_thread_id}`. Files from earlier turns are never found this way. <!-- verify:ignore -->
- `setup_agent_tools` uses only the first entry of `pre_tools_data`. In `handle_pre_tools`, a `rag_knowledgebase` pre-tool without `resource_id`/`collection_id` (or an unknown type) is skipped, and the default `entry` `{"id": tool_id, "type": "pre_tool"}` is still saved to `pre_tool_response_to_save`. Built-in pre-tools carry no `id` or `name`, so that stub is keyed `None`.
- A `custom_function` pre-tool and the post-tool POST to their stored `url` (from the `connected_tools` entry or its apicalls document, set in `getConfiguration`), with no viaSocket flow.sokt.io default. With no `url`, `axios_work` returns `status: 0` and the error text becomes the `pre_function` variable or the post-tool's history entry. `handle_post_tool` still skips a post-tool that has no `script_id` / `function_name` / `endpoint_name`, even when it has a `url`.
- The fallback model's prompt lines are lost: in `chat`'s fallback, `normalize_response_type` may set `_json_schema_instruction` for the fallback model, but nothing appends it to the prompt (BaseService strips the key). A fallback model that does not list `json_object` gets `response_type` dropped with no JSON instruction, unless the primary already added one. `model_supports_response_type` also returns `False` when the model's `response_type` has no `options` list, despite its docstring.
- The `query_refiner` pre-tool calls `https://api.gtwy.ai/api/v2/model/chat/completion` (`call_ai_middleware`), the production API, from every environment.
- For streamed requests, `chat`'s `finally` marks the API key "working" before the stream runs, so stream-time provider errors never change the key status.
- `repair_json` rarely raises; invalid JSON from the model is silently replaced with its "repaired" guess.
- In `check_transfer_from_codes_mapping`, a transfer tool whose name is missing from `tool_id_and_name_mapping` hits an unbound `agent_id`.
- `image` never initializes `params`, so an error before `build_service_params` raises inside its own `except` and no error row is saved.
- `/openai/responses` only works for synchronous, default-format calls: a `StreamingResponse` or queued reply cannot be formatted. All text in an `input` list (any role) is joined into one `user` message; multi-turn context needs `conversation_id`/`thread_id`. A missing `agent_id` gives a 500 with a traceback (`_extract_agent_identifier` calls `.strip()` on `None`, and `FastAPI(debug=True)` returns the traceback), not the intended 400.
- `Config.max_workers` is passed to `int()` at import time in this router (the `executor` is unused here). An unset `max_workers` crashes startup.
- History from Redis is keyed by `version_id`, but the Postgres fallback is keyed by `bridge_id`, so after the 2 h cache expires a thread can pick up turns from other versions of the agent.

## Why it is built this way

- **Sticky agent per thread** (2dc40064): once a conversation is transferred, follow-up turns go straight to the agent that answered, keyed by the original primary agent so the lookup is stable.
- **Owner token on the tool budget** (ba0ebe08, after the first try c28ed7f3 was reverted in 908511b3): an agent that calls itself as a tool re-enters with the same key; without the token the child's `finally` wiped the parent's budget. Streaming needs `cleanup_tool_count_after` because `chat` returns before the loop runs.
- **Single-use hold token** (38aacc53 comments in `chat` and `chat_multiple_agents`): earlier code double-released on queue redelivery and leaked the hold when `parse_request_body` failed before the `finally` variables existed.
- **Seed the conversation cache on the first turn** (`_update_history_redis` comment): before, a new thread cached nothing, and without the Node log-queue worker its second turn had no memory.
- **Conversation key falls back to `bridge_id`** (d37dcc8a): the hidden direct agent has no version, and `None_<thread>` keys were shared across orgs.
- **History is saved by Node, not here**: Python only publishes to the log and metrics queues (`process_background_tasks` docstring), so the gtwy-node consumer owns the Postgres write.
- **`json_schema` downgrade**: models that cannot enforce a schema get it inlined into the prompt plus plain `json_object`, instead of dropping the format (`normalize_response_type`). Since 6575315b `json_object` is checked per model too: providers only accept the bare `{"type": "json_object"}` and reject a type the model does not support, so a stored schema moves into the prompt and an unsupported type is dropped.
- **Fallback on truncation** (2f93a7a4): answers cut off by `max_tokens` or the context window used to reach the user as is. The primary answer is kept when the fallback fails because it is usable, only incomplete.
- **Nested streamed agents never emit `error`** (b4ad8d70): a nested agent shares its caller's stream, and an `error` event ends the whole response on the client. The caller gets the failure as the agent's tool result instead.
- **Rejected**: a WebSocket chat endpoint with `session_manager.py` and a LangChain `workflow/` package for planning were removed in c5be5b08; planning now lives in `src/services/todo/`. <!-- verify:ignore -->

> [!TODO] Needs a human: why did 52e28a34 replace `return` with `raise` after a streamed transfer in `sse_stream_and_finalize`? Is the error event on transfer intended?

## Changing it safely

- **New body field the provider needs**: add it in `parse_request_body`, pass it in `build_service_params` (and `build_service_params_for_batch` if batch needs it), read it in BaseService `__init__` ([tool-calling](tool-calling.md)). Example: 6c6f2d72 added `thread_flag` this way.
- **Anything in the response tail** (cost, reviewer, post-tool, history): change both `chat` and `sse_stream_and_finalize`; they are parallel copies.
- **New SSE event**: add an `emit_*` method on `StreamingService`, keep `done` last, and only `close()` when not `_nested_stream_call`. The frontend parses `event` from the JSON.
- **New pre-tool type**: produce it in `getConfiguration` (`pre_tools_data`), add a branch in `handle_pre_tools` that replaces the default `entry` with one that has a real `id` (built-in types use a fresh uuid), and write the result into a variable the prompt can use.
- **Log-queue payload keys** (`save_history`, `metrics_service`, `handle_gpt_memory`, `save_files_to_redis`, `billing`, …): gtwy-node's log-queue consumer reads them. Change both repos together.
- There are no automated tests for this module. Check a change by running a sync call, a streamed call, a call with `fall_back` and a forced provider error, a transfer between two agents (streamed and not), and a queued (webhook) call, then confirm one history row per agent and that the hold was released.
