---
covers: ["src/services/commonServices/baseService/**", "src/services/commonServices/api_executor.py", "src/services/mcp_gateway/**", "src/services/utils/mcp_utils.py", "src/services/utils/built_in_tools/firecrawl.py", "src/services/utils/apiservice.py", "src/services/utils/ai_call_util.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Tool calling

`BaseService` is the parent class of every provider handler (`OpenaiResponse`, `Anthropic`, `GeminiHandler`, `OpenAICompatibleHandler`, ...). It does three jobs. It shapes the provider request (`service_formatter`). It sends the request (`chats` or `stream`). It runs the tool loop (`function_call`): read the tool calls in the reply, run them all at once, add the results to the conversation, then call the model again. The provider handlers in [llm-providers](llm-providers.md) call it. [chat-orchestration](chat-orchestration.md) creates the handler and sets the loop budget.

| File | Role |
|---|---|
| `src/services/commonServices/baseService/baseService.py` | `BaseService`: request shaping (`service_formatter`), provider dispatch (`chats`, `stream`), the loop (`function_call`, `run_tool`, `update_configration`), the history row (`prepare_history_params`) and the error row (`handle_failure`). |
| `src/services/commonServices/baseService/utils.py` | Helpers that branch on wire format (`validate_tool_call`, `make_code_mapping_by_service`, `tool_call_formatter`, `disable_tool_call`, `build_accumulated_response`, `run_stream_and_collect`) and tool execution (`process_data_and_run_tools`, `_run_tool_tasks`, `axios_work`). It also holds shared helpers from other areas that live here for historical reasons: `sendResponse`, `make_request_data`, `compute_billing_events`, `make_request_data_and_publish_sub_queue`, `makeFunctionName`, `unknown_error_handler_alert`. |
| `src/services/commonServices/api_executor.py` | `execute_api_call` wraps every non-stream provider call: it deep-copies the config, times the call, runs `check_space_issue`, counts usage, and sends an alert on failure. |
| `src/services/mcp_gateway/transport.py` | `open_mcp_session` tries streamable HTTP first, then falls back to SSE. |
| `src/services/mcp_gateway/client.py` | `call_mcp_tool` opens one session per call, runs `call_tool` and flattens the content to text. |
| `src/services/mcp_gateway/prefetch.py` | `prefetch_mcp_tools` runs `list_tools` for each server and caches the result in Redis for 10 minutes. |
| `src/services/utils/mcp_utils.py` | MCP naming (`build_mcp_tool_name`, `display_mcp_tool_name`). The client/server strategy (`resolve_mcp_type`, `client_mcp_config`, `server_mcp_config`). Reading MCP calls the provider already ran (`extract_server_side_mcp_calls`, `merge_server_side_mcp_into_tools`). |
| `src/services/utils/built_in_tools/firecrawl.py` | `call_firecrawl_scrape`, the `Gtwy_Web_Search` built-in tool. |
| `src/services/utils/apiservice.py` | `fetch` / `fetch_stream`, the shared aiohttp client with retries. HTTP tools, Firecrawl, webhooks and several provider runners use it. |
| `src/services/utils/ai_call_util.py` | `call_gtwy_agent` (agent as a tool, run in the same process) and `call_ai_middleware` (an HTTP call to a platform agent on production gtwy). `generate_token` and `get_ai_middleware_agent_data` have no callers. |

## How it works

### Request shaping and dispatch

1. A handler's `execute()` builds `self.customConfig` (messages or input), then calls `BaseService.service_formatter(self.customConfig, service)`. This function renames keys for the service ([llm-providers](llm-providers.md)) and also does all the tool work:
   - It turns client-side MCP servers into function tools (`client_mcp_config`). This mutates the input config and `tool_id_and_name_mapping`.
   - `tool_call_formatter` converts the gateway tool list into the provider's schema.
   - It formats `tool_choice`. For openai_completion, groq, grok and deepseek, a tool name becomes `{"type": "function", "function": {"name": …}}`. Anthropic defaults to `{"type": "auto"}`.
   - It attaches server-side MCP servers (`server_mcp_config`).
   - If this agent's tool budget is already 0 (`get_tool_count`), it disables tool calls (`disable_tool_call`).
2. The handler calls `chats` (non-stream) or `stream`. `chats` branches on `service` to the provider runner. Every service where `uses_openai_sdk` is true shares `openai_compatible_modelrun`. Anthropic runs in the module-level `ThreadPoolExecutor` through `asyncio.run`, so each call gets a new event loop. Non-stream runners go through `execute_api_call`. Both methods return `{"success": True, "modelResponse": <provider response as a dict>}`. Failures raise `ApiCallError`.
3. `stream` sends the SSE start event only when `count == 0`. `run_stream_and_collect` reads the provider's delta generator and emits content, reasoning and tool-call events as they arrive. If the stream was empty, or dropped before anything was emitted, `stream` retries the same model once (`STREAM_SAME_MODEL_MAX_RETRIES`). Then `build_accumulated_response` rebuilds the reply in the non-stream shape. From this point on, stream and non-stream work the same way.
4. The handler calls `function_call(self.customConfig, service, response, 0, {})`, then `update_model_response`, `Response_formatter` and `prepare_history_params`.

### The tool loop

`BaseService.function_call` calls itself once per round:

1. If `tool_choice` forces a tool (anything except `auto`/`none`), it is reset to auto, so the model is not forced to call that tool again.
2. `validate_tool_call(service, model_response)` checks for tool calls. If there are none, the function returns the response. **This is the only way out of the loop.**
3. `decrement_tool_count(build_tool_count_key(bridge_id, message_id))` spends one unit of budget. When the remaining budget reaches 0 or less, `disable_tool_call` turns tool calls off for the *next* model call (tool_choice `none`, or Gemini function calling mode `NONE`) and sets `maximum_iteration_limit_reached`. The tools of the current round still run.
4. `run_tool`:
   1. `make_code_mapping_by_service` reads the calls into `codes_mapping`. If the args are not valid JSON, they become `{"error": "<raw>"}` with `error: true`.
   2. It sends a `function_call` progress event through `sendResponse`. It does not send this event when the response format is webhook. In practice only RTLayer receives it.
   3. `replace_variables_in_args` writes request variables into the args, at the dot paths in `variables_path[<mapping name>]`. The mapping name is the viaSocket `script_id` for flow tools, and the `bridge_id` for agents.
   4. `check_transfer_from_codes_mapping` looks for a call with `action_type == "transfer"` (only orchestrator agents expose this arg). If it finds one, nothing runs and the loop returns `transfer_agent_config` ([chat-orchestration](chat-orchestration.md#agent-transfer)).
   5. `process_data_and_run_tools` runs the round (see below).
5. In stream mode, `function_call` emits one `emit_tool_result` per result.
6. `update_configration` adds the assistant turn and the results to the conversation, in the provider's format (see the table below). It also records `tools[display_name] = content`.
7. `function_call` calls `stream` or `chats` again with `count = loop_count`, puts `tools` on the new response, and calls itself.

`chat()` sets the budget with `init_tool_count`: `maximum_iterations`, default 3, at least 25 when `Gtwy_Browser` is on ([chat-orchestration](chat-orchestration.md)). With a budget of N, the model gets up to N rounds of tool results, then one last call with tools off. The response then has `finish_reason = "maximum_iteration_limit_reached"`. `BaseService.maximum_iterations` is stored but the loop never reads it.

### From tool name to executor

`tool_id_and_name_mapping` maps the tool name the model sees to how the gateway runs it. `src/services/utils/getConfiguration_utils.py` builds it for each request ([agent-configuration](agent-configuration.md)), and `client_mcp_config` adds the MCP tools. `process_data_and_run_tools` picks the executor from the entry's `type`:

| `type` in the mapping entry | Registered by | Executor | `metadata.type` of the result |
|---|---|---|---|
| none (HTTP tool) | `process_api_call_tool` (an `apicalls` tool: always POST to the document's `url`; before 6fdb8181 the URL was built as flow.sokt.io/func/<script_id>) and `process_extra_tool` (request `extra_tools`: any URL, method, `query_params`) | `axios_work` → `fetch`, capped at 60 s by `SERVICE_TIMEOUTS` | `function` |
| `RAG` | `add_rag_tool` (tool `get_knowledge_base_data`) | `get_text_from_vectorsQuery` ([media-rag-chatbot](media-rag-chatbot.md)) | `RAG` |
| `AGENT` | `add_connected_agents` | `call_gtwy_agent` | `agent` |
| `Gtwy_Web_Search` | `add_web_crawling_tool` | `call_firecrawl_scrape` | `function` |
| `Gtwy_Browser` | `add_browser_tool` | `call_gtwy_browser` ([gtwy-browser](gtwy-browser.md)) | `function` |
| `MCP` | `client_mcp_config` | `call_mcp_tool` | `mcp` |

If a name is not in the mapping, nothing runs and the model gets `"Wrong Function name"`. Gemini sometimes adds a prefix to names with a dot. When the full name is missing, the gateway tries the part after the last dot.

For HTTP tools, `resolve_url_params` fills `:param` and `{param}` in the URL from the args. On GET, the other args become query params. On other methods, the keys listed in `query_params` become query params and the rest is the JSON body.

The loop never sees these tools, because the provider runs them: provider-native built-ins (OpenAI `web_search` / `image_generation`, Anthropic `web_search_20250305`, added by the handlers in [llm-providers](llm-providers.md)) and server-side MCP. Pre-tools run before the first model call ([chat-orchestration](chat-orchestration.md)) and reuse `call_firecrawl_scrape` and `call_ai_middleware`.

### Running one round

`process_data_and_run_tools(codes_mapping, self)`:

- **Testcase replay.** When `run_testcase` is set and the tool name is in `testcase_tools_response`, `get_saved_tool_response` returns the recording whose args match exactly. If none matches, it returns an error payload. The real tool is never called ([testcases](testcases.md)).
- **Execution.** Every other call becomes a coroutine. `_run_tool_tasks` runs them all with `asyncio.gather(..., return_exceptions=True)`, with one exception: `Gtwy_Browser` calls share one tab, so they run one after another while the other tools run in parallel. Results come back in call order. The number of parallel calls in a round has no limit.
- **Result to content.** An exception becomes `{"error": "Error during async task execution", "details": …}`. Bad-JSON args give `{"error": "Args / Input is not proper JSON"}`. `status == 1` gives `response`. Any other status gives `{"error": response}`. The model receives `json.dumps(...)` of that value.
- The function returns `(responses, mapping_by_call_id, tool_call_logs)`. `run_tool` passes them on as `func_response_data`, `mapping_response_data`, `tools_call_data`, and `function_call` appends `tools_call_data` to `self.func_tool_call_data`. `prepare_history_params` saves that list as `tools_call_data`.

### Feeding results back

`update_configration` writes into the provider's own conversation key:

| Wire format | Appended for each round |
|---|---|
| openai_chat (`choices[0].message`) | For **each** call, an assistant message holding only that call (plus `reasoning_content` when present, which Moonshot thinking mode needs), then its `role: tool` message. |
| openai_responses (`openai`) | For each call: every `reasoning` output item, then the `function_call` item, then `{"type": "function_call_output", "call_id", "output"}`. In stream mode, items go through `sanitize_openai_response_item`. |
| anthropic | Once: the assistant `content` as it came, then one user message whose content lists one `tool_result` block per call. |
| gemini | Once: the candidate content, without the synthetic `gemini_fc_` ids. Then one user `types.Content` per call, built with `Part.from_function_response(name, {"result": <parsed content>})`. |

The wire format comes from the DB-driven service registry (`has_openai_choices_shape` and the related functions in `src/configs/service_registry.py`). Every helper in this area that depends on the format branches on these functions, not on service names.

### Agent as a tool

`call_gtwy_agent` calls `chat()` in the same process, with no HTTP call. The child request starts from the pre-fetched `bridge_configurations[bridge_id]` and adds:

- `user` (the `_query` arg) and `variables` (every arg except `user`)
- the parent's `message_id` and `billing_attribution`
- `_nested_agent_call: true`
- `configuration.stream`, equal to the parent's stream mode. A streaming child writes into the parent's SSE stream through `_injected_streamer`.
- thread ids, only when the connection sets `thread_id`

The child's budget key is `<child bridge_id>-<message_id>`, so it does not spend the parent's budget. The result carries `service`, `model`, `usage` and `tools_data`. `merge_nested_agent_usage` folds them into the parent's `tools_data` entry, so cost stays visible at each level. The model sees only the parsed reply.

### MCP

`resolve_mcp_type` picks a strategy per model from `validationConfig.mcp_type` in the model config: `server` or `client`. Anything else counts as `client`.

- **client.** `chat()` calls `prefetch_mcp_tools`, which fills `server["tools"]` through `list_tools`. The result is cached under the Redis key `mcp_tools_list:` + sha256(url, headers) for 600 s. If the call fails, the server gets no tools and the request continues. `client_mcp_config` exposes each tool as a function named `<tool>__mcp` (one server) or `<tool>__<server>__mcp` (several servers) and registers an `MCP` mapping entry. `call_mcp_tool` opens a new session for every call. Clients, streams and `tools_data` get the name from `display_mcp_tool_name`, which strips the suffix.
- **server.** `server_mcp_config` hands the servers to the provider. For openai, groq, grok and mistral it adds a tool `{"type": "mcp", "server_label", "server_url", "require_approval": "never", "headers"}`. For Anthropic it sets `mcp_servers` and the MCP client beta, with the bearer token taken from the Authorization header. Other services log a warning and get nothing. The provider runs the calls. `extract_server_side_mcp_calls` copies them from the response into `tools_call_data` entries marked `source: "server_side"`.

### HTTP client

`fetch(url, method, headers, params, json_body)` returns `(parsed_json, response_headers)`. It opens a new session for every call, with a 600 s total timeout and a 15 s connect timeout. It retries 3 times (after 1 s, 2 s, then 4 s) on HTTP 502/520 and on transient network errors (`ClientPayloadError`, connection errors, timeouts). A status of 300 or more raises `ValueError({"error": text, "status_code": n})`. A 502/520 also fires `unknown_error_handler_alert`. `fetch_stream` retries only if no line has been yielded yet.

## Contracts

```python
# make_code_mapping_by_service → codes_mapping (keyed by the provider's call id)
{"call_abc": {"name": "get_weather", "args": {"city": "Pune"}, "error": False}}

# tool_id_and_name_mapping entries (getConfiguration_utils.py, mcp_utils.py)
{"get_weather":   {"url": "<apicalls.url>", "headers": {}, "name": "<script_id>", "method": "POST"},
 "my_api":        {"url": "...", "headers": {...}, "name": "my_api", "method": "GET", "query_params": ["page"]},  # extra_tools
 "Sales_Agent":   {"type": "AGENT", "bridge_id": "...", "requires_thread_id": False},
 "get_knowledge_base_data": {"type": "RAG", "resource_to_collection_mapping": {"<resource_id>": "<collection_id>"}},
 "search__mcp":   {"type": "MCP", "name": "search__mcp", "mcp_server": "docs", "mcp_tool": "search",
                   "mcp_url": "...", "mcp_headers": {...}}}

# Every executor returns this (never None). status 0 → the model sees {"error": response}.
{"response": <any JSON-able>, "metadata": {"type": "function" | "agent" | "mcp" | "RAG", ...}, "status": 1}
# call_gtwy_agent adds: "service", "model", "usage", "tools_data"; metadata has agent_id, version_id, message_id.

# func_response_data item (what the model gets back, before provider formatting)
{"tool_call_id": "call_abc", "role": "tool", "name": "get_weather", "content": "{\"temp\": 31}"}

# tools_call_data: one dict per round, saved on the history row
[{"call_abc": {"name": "<display name>", "args": {...}, "error": False, "model_tool_name": "...",
               "display_tool_name": "...", **mapping_entry, "data": <executor result>, "id": "<script_id | mcp tool | name>"}}]

# function_call return: the LAST model response
{"success": True, "modelResponse": {...}, "tools": {"get_weather": "{\"temp\": 31}"},
 "transfer_agent_config": {...}}   # only on transfer
```

- gtwy-node reads `tools_call_data` straight from Postgres for its history filters and analytics. It reads `id`, `name`, `display_tool_name`, `bridge_id`, `args.resource_id`, `data.metadata.agent_id` and `data.metadata.type`. If you rename any of these, history filtering and per-tool analytics break in the other repo.
- `tools` (`tools_data` in the API response) is keyed by display name. Agent entries are the merged dict from `merge_nested_agent_usage`, not the raw string. `Response_formatter` JSON-parses string values when it can.
- `sendResponse(response_format, data, success)` delivers to `RTLayer` or `webhook` and does nothing for other types. For webhooks it stores the customer's reply on `response_format["webhook_response"]` (a mutation, on purpose).

## Gotchas

> [!WARNING]
> **The budget only turns tools off; it never breaks the loop.** `function_call` stops only when a reply has no tool calls. If a provider ignores tool_choice `none`, the loop keeps going, and every round runs the tools again. If you add an entry point that skips `init_tool_count`, `decrement_tool_count` returns 0 on the first round, so you get exactly one tool round.

> [!WARNING]
> **Bad-JSON args still run the tool.** The `error` flag from `make_code_mapping_by_service` is checked only *after* the round has run. An HTTP tool gets the body `{"error": "<raw args>"}`. Then the model gets "Args / Input is not proper JSON" instead of the real response.

> [!WARNING]
> **A transfer drops the other calls in its round.** When any call has `action_type: "transfer"`, `run_tool` returns before running anything, including unrelated parallel calls.

> [!WARNING]
> **`fetch` needs JSON and retries POSTs.** `response.json()` raises on a 2xx response with a non-JSON content type, so an HTTP tool that returns text/plain is reported to the model as an error. On 502/520 and on dropped connections, `fetch` replays the whole request up to 3 times, so a non-idempotent tool can run more than once. `axios_work` caps the total at 60 s. On that timeout the model gets `{"error": ""}`, because `str(TimeoutError())` is empty. `call_mcp_tool` and `call_gtwy_agent` have no timeout of their own.

> [!WARNING]
> **Secrets reach history.** Each `tools_call_data` entry spreads the whole mapping entry, including `headers` (for `extra_tools`) and `mcp_headers` (MCP auth). Python does not strip them before `save_history`. Do not put new credentials in a mapping entry without removing them in `process_data_and_run_tools`.

> [!WARNING]
> **Errors inside an MCP session come out disguised.** `open_mcp_session` wraps its `yield` in `try/except Exception`. So an exception raised inside the caller's `async with` body (for example by `session.call_tool`) triggers the SSE fallback. `call_mcp_tool` then reports the SSE failure, or a `RuntimeError`, instead of the original error. A tool result with `isError` is not affected.

> [!WARNING]
> **Same tool twice means one `tools_data` entry.** `tools[display_name]` is overwritten when a tool runs twice (in parallel or across rounds). `tools_call_data` keeps every call. Gemini non-stream calls are keyed by the provider's `function_call.id`, and only the stream path adds `gemini_fc_` ids. So if Gemini omits ids, parallel calls collide on the key `None` and only the last one runs.

> [!WARNING]
> **A new service needs a branch in `chats` and `stream`.** A service that matches no branch leaves `response = {}`. `chats` then fails with `KeyError: 'success'`, which is wrapped as `ApiCallError("error occurs from … api: 'success'")`. `stream` raises "Streaming not supported".

> [!WARNING]
> **Anthropic non-stream runs on another event loop.** `chats` runs `anthropic_runmodel` in a worker thread through `asyncio.run`. Objects bound to the main loop (the async Redis client, the request streamer) must not be used inside it. Startup also runs `int(Config.max_workers)` at import time, so the `max_workers` env var is required ([environment](../environment.md)).

> [!WARNING]
> **`unknown_error_handler_alert` must not use `fetch`.** `fetch` calls it on 502/520. When it used `fetch` itself, a failing alert endpoint looped forever (fixed in c387ef3d "loop break"). Keep the raw aiohttp call.

> [!WARNING]
> **`call_ai_middleware` always calls production** (the gtwy production chat completion URL, with `AI_MIDDLEWARE_PAUTH_KEY`), from every environment. It powers platform agents such as `bridge_ids["query_refiner"]`, test cases and the prompt optimizer. If you change the URL, the platform agent ids must exist on the new target.

> [!WARNING]
> **MCP display names split on `__`.** `display_mcp_tool_name("my__tool__mcp")` returns `my`. Also, MCP tool names are not cleaned with `makeFunctionName`, so a server name with spaces produces a function name the provider may reject.

## Why it is built this way

- **The budget disables tools instead of failing.** Until ba0ebe08 (#1535), going over `maximum_iterations` stopped the run and returned "Execution stopped in between because tool call limit exceeded". Now the last round's results reach the model, which must answer without tools. The first attempt (c28ed7f3) was reverted in 908511b3. The version that landed adds `cleanup_tool_count_after`. Its docstring explains why: for streams, the caller's `finally` would otherwise clear the counter before the loop runs ([chat-orchestration](chat-orchestration.md)).
- **An agent as a tool is an in-process `chat()`.** Before 925fd490 it was an HTTP call to the gtwy API (localhost, dev or prod, chosen by env) with a JWT. 9e9f7aa2 then switched from calling `getConfiguration` per child to using the parent's pre-fetched `bridge_configurations`. A child agent missing from that dict fails with a `KeyError` message as its tool result.
- **Browser calls run one after another** (cdd31179). The calls of one conversation drive the same tab, so running them in parallel would interleave clicks and snapshots.
- **Gemini synthetic ids.** The stream runner gives each function call a `gemini_fc_<uuid>` id so the loop can match results to calls. `update_configration` removes the ids again before sending the content back to Gemini.
- **Same-model stream retry happens before fallback** (2ea78538). An empty or dropped stream is retried once on the same model, before the model-level fallback. The retry happens only when nothing has reached the client yet, so tokens are never sent twice (see the comments in `stream`).
- **`fetch` retries** (478bbde1). Upstreams and proxies closed sockets mid-body ("Response payload is not completed"). Retrying with a new connection fixed it. `fetch_stream` does not retry after the first yielded line, so it never duplicates SSE lines.
- **`api_executor` has no retry.** It was `retry_mechanism` until 81820bdf. Fallback now lives in [chat-orchestration](chat-orchestration.md). The `alert_on_retry` parameter name is left over from that time.

> [!TODO] Needs a human: should `call_ai_middleware` keep calling production from testing and local environments, or is that only because the platform agents in `bridge_ids` exist only there?

> [!TODO] Needs a human: why does non-stream Anthropic run in a `ThreadPoolExecutor` with `asyncio.run` (added in c35ebc97, no message), when `anthropic_runmodel` already uses `AsyncAnthropic`?

## Changing it safely

**Add a new tool kind** (example: Gtwy_Browser, cdd31179; MCP, 5212c3d3):

1. Give it a type name in `src/configs/constant.py` (`inbuild_tools` or `tool_types`).
2. Register the schema and a mapping entry `{"type": <name>, …}` in `src/services/utils/getConfiguration_utils.py`. Use the gateway tool shape `{"type": "function", "name", "description", "properties", "required"}`. `tool_call_formatter` converts it for every provider, so do not write provider formats yourself.
3. Write an executor that never raises and always returns `{"response", "metadata": {"type": …}, "status": 0|1}`. `process_data_and_run_tools` turns any other return value into a crash for the whole round. Pick `metadata.type` with gtwy-node analytics in mind: `agent` and `RAG` get their own buckets, and everything else counts as a function.
4. Add an `elif` for the type in `process_data_and_run_tools`. If calls must not overlap, add the type to the sequential branch in `_run_tool_tasks`.
5. If the result needs an `id` for history filters, make sure the mapping entry has `name` (or `mcp_tool`).

**Add a provider.** Most of this area branches on wire format, so a service with an existing `wire_format` only needs a branch in `chats` and `stream`, unless `uses_openai_sdk` is true. A new wire format needs a new case in `validate_tool_call`, `make_code_mapping_by_service`, `tool_call_formatter`, `disable_tool_call`, `update_configration`, `build_accumulated_response` and `check_space_issue`. See [llm-providers](llm-providers.md).

**Check your change.** This area has no automated tests. Call `POST /api/v2/model/chat/completion` for an agent that has the tool, with `stream` off and then on. Check `response.data.tools_data`, the SSE `tool_call` / `tool_result` events, and the `tools_call_data` on the saved history row. Set the agent's `settings.maximum_iterations` to 1 and confirm that the second model call runs without tools and that `finish_reason` is `maximum_iteration_limit_reached`.
