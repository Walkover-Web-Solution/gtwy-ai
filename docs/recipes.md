---
covers: ["src/configs/constant.py", "src/schemas/completion_schemas.py", "src/services/commonServices/baseService/baseService.py", "src/services/commonServices/baseService/utils.py", "src/services/utils/helper.py", "src/services/utils/ai_middleware_format.py", "src/services/utils/token_calculation.py", "src/services/utils/getConfiguration.py", "src/services/utils/getConfiguration_utils.py", "src/services/utils/common_utils.py", "src/services/commonServices/common.py", "src/middlewares/getDataUsingBridgeId.py", "src/middlewares/ratelimitMiddleware.py", "src/routes/**", "index.py", "config.py", ".env.example", "src/services/cache_service.py", "src/services/cache_utils.py", "src/utils/alert_utils.py", "src/utils/alert_template.py", "src/services/utils/time.py", ".pre-commit-config.yaml", ".cz.toml"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Recipes

Step-by-step for the changes this repo sees most often. Each recipe comes from real commits and was checked against the code at af2a2886. Most bugs in the history come from a missed registration step or a missed parallel copy, so do every step.

## Before you start

- **Two PRs per change.** A change is merged into `testing` first, then the same commit goes to `master` in its own PR. `.github/workflows/alert.yml` announces a DEV deploy on a push to `testing` and a PROD deploy on a push to `master`. TypeSafe went in as 12fe9008 (#1785) on testing and b13a5c11 (#1784) on master. Keep the two PRs identical: 931fd0b3 only exists because master got an older copy than the one tested on testing.
- **"Testing (#NNNN)" commits** (38aacc53, 479fe5a0, f7713ce4) are squashed testing-to-master merges that carry many PRs. Read them with `git show --stat`; the subject tells you nothing.
- **Lint and commit messages.** ruff (`--fix` + format) and commitizen (`type(scope): subject`) run as pre-commit hooks, and no CI runs either. Install both hooks and run them yourself: [environment](environment.md#lint-and-commit-tooling). About half of the recent subjects ("add route", "large image fix") break the commit rule. Do not copy them.
- **No test suite.** Every recipe ends with a manual check against a local server. Start it as in [environment](environment.md#run-it-locally), and turn `CONSUMER_STATUS` off (or use private queue names) so it does not take jobs from a shared queue. Authenticate as in [api](api.md#auth): the raw JWT in `Authorization`, with no `Bearer ` prefix.
- `POST /api/v2/model/ai/completion` runs any service and model without creating an agent. It is the quickest way to call a provider:

```bash
curl -s localhost:8080/api/v2/model/ai/completion \
  -H "Authorization: $JWT" -H 'Content-Type: application/json' \
  -d '{"service":"<service>","configuration":{"model":"<model>"},"user":"Say hi"}'
# success: {"success": true, "response": {"data": {"content": "..."}, "usage": {..., "cost": ...}}}
# add "stream": true to the body to get text/event-stream instead
```

## Add an OpenAI-compatible LLM provider

Use this when the provider speaks the OpenAI Chat Completions API and `AsyncOpenAI` with a custom `base_url` works. Example commit: 66a19c42 "fix: service minimax (#1683)". Background: [llm-providers](modules/llm-providers.md#changing-it-safely).

1. In gtwy-node, write a migration that upserts the `services` document (`wire_format` openai_chat, `client` openai_sdk, `base_url`, `service_keys.default`, `apikey_status_codes`, the `supports_*` flags) and the `modelconfigurations` documents with `status: 1`. Mongo schemas live in that repo. The TypeSafe migration there is a good template.
2. Add the key to `service_name` in `src/configs/constant.py`.
3. Add the same string to `_SERVICE_NAMES` in `src/schemas/completion_schemas.py`. The comment there says "keep in sync" with `service_name`. Nothing checks it.
4. Add the service to the hard-coded list in `src/services/commonServices/baseService/baseService.py:update_model_response`.
5. Nothing else. `src/services/utils/helper.py:create_service_handler` routes the service to `OpenAICompatibleHandler` through `uses_openai_sdk`. `chats` and `stream` use the same predicate. `Response_formatter` falls through to `format_openai_compatible`, and the `case _` in `calculate_usage` reads usage for any service where `has_openai_choices_shape` is true.
6. Check it works. Deploy the migration (the registry updates live through `background_listen_for_service_changes`, so no restart is needed). Restart the Python server for the code change. Then call `POST /api/v2/model/ai/completion` with the new service, once without `stream` and once with it. Confirm a non-zero `response.usage.cost`, then run one tool call through an agent that has a tool.

> [!WARNING]
> A DB-only insert looks like it works and then fails three ways. The docstring in `openai_compatible_modelrun.py` says otherwise, but it is wrong. Without step 2, `service_formatter` raises "Service key error" whenever `stream` is in the config, and `CompletionRequest.validate_known_service` rejects the service. Without step 3, a request body that names the service gets a 422. Without step 4, the final response after a tool loop keeps stale `tool_calls`.

> [!WARNING]
> Only `client` openai_sdk has a handler. `Client.OPENAI_COMPLETION_SDK` and `Client.MINIMAX_SDK` exist, but nothing branches on them. A service that uses either value gets "Unsupported service".

## Add an LLM provider with its own API

Use this for a new wire format or SDK. Example commit: b13a5c11 "feat: add TypeSafe (Jev) as a service (#1784)", with the follow-up 931fd0b3. Its body says it follows the Deepgram pattern (cf0a2eec). Background: [llm-providers](modules/llm-providers.md).

1. `src/configs/constant.py`: add a `WireFormat` member, a `Client` member and the `service_name` key. Add the name to `_SERVICE_NAMES` in `src/schemas/completion_schemas.py`.
2. Add a new folder under `src/services/commonServices/` with a Call class and a runner. Copy `src/services/commonServices/typesafe/`. The Call class subclasses `BaseService` and implements `execute` (`src/services/commonServices/typesafe/typesafeCall.py:TypeSafe`). The runner wraps the HTTP call in `execute_api_call` (`src/services/commonServices/typesafe/typesafeModelRun.py:typesafe_runmodel`). Skipping `execute_api_call` skips usage calculation, so the call bills $0.
3. Add a branch in `src/services/utils/helper.py:create_service_handler`. It must come before the `uses_openai_sdk` branch, as the deepseek and typesafe branches do.
4. Add a branch in `src/services/commonServices/baseService/baseService.py:chats`. If the provider streams, also add one in `stream`.
5. Add a formatter module in `src/services/utils/formatters/` and a branch in `src/services/utils/ai_middleware_format.py:Response_formatter`. With no matching branch, the formatter returns `None` and the caller crashes on `result["response"]`.
6. Add a `case` in `src/services/utils/token_calculation.py:calculate_usage` that maps the provider's usage to `inputTokens`, `outputTokens` and `totalTokens`.
7. If the provider supports tool calls, follow the wire-format list in [tool-calling](modules/tool-calling.md#changing-it-safely) as well.
8. Add the gtwy-node migration with the `services` and `modelconfigurations` documents, as in the OpenAI-compatible recipe. Put the request field map in `service_keys.default`.
9. Check it works: run the same `POST /api/v2/model/ai/completion` checks as for an OpenAI-compatible provider. Also send one request that you expect the provider to reject, and confirm the error text reaches `response.error`.

> [!WARNING]
> Send the provider only the fields it accepts. TypeSafe returns 422 for unknown fields and answers an unsupported question type with a bare "Invalid request." (b13a5c11, 931fd0b3). Its handler builds the request body explicitly instead of forwarding the config.

**Add a model to an existing provider: no code.** Insert a `modelconfigurations` document from gtwy-node with `status: 1`. Only status-1 documents load (95f90257). A model missing from `model_config_document` returns 400 "model or service does not exist!" from `add_configuration_data_to_body`.

## Add an HTTP endpoint

Example commits: d37dcc8a "feat: AI completion API without a user-created agent (#1794)" (a model route on the full pipeline), 7cb8aaf4 "feat: added pydantic validation on non-completion routes (#1301)" (schemas on other routers). The route catalogue is [api](api.md).

1. Put the route in the router that owns the prefix. Model routes go in `src/routes/v2/modelRouter.py`, mounted at `/api/v2/model`. A new router file must also be mounted with `app.include_router(..., prefix=...)` in `index.py`. If you forget, the route is a silent 404.
2. Declare auth as a dependency: `dependencies=[Depends(...)]`. Copy `auth_and_rate_limit` (JWT, then 100/min per agent and 20/min per thread) or `direct_auth_and_rate_limit` (per org).
3. Put the body model in `src/schemas/`. Either type a route parameter with it (as in `src/routes/rag_routes.py`), or call `model_validate` and re-raise `RequestValidationError` (as `direct_auth_and_rate_limit` does). Both produce the 422 shape from `index.py:validation_exception_handler`. A bare `ValidationError` becomes a 500 with a traceback, because the app runs with `FastAPI(debug=True)`.
4. If the route runs an agent, depend on `add_configuration_data_to_body(<YourSchema>)` and call the same pipeline function that `chat_completion` calls. That dependency reserves credits. Every exit path must release the hold, so do not stop between reserving and running the pipeline.
5. Keep handler logic in `src/controllers/` or `src/services/`. Return a dict or a `JSONResponse`, and raise `HTTPException` for client errors.
6. Document the route in [api](api.md).
7. Check it works: call the route with a valid body (200), with a broken body (422 `{"detail": "Validation error", ...}`), without `Authorization` (`jwt_middleware` returns 404 "unauthorized user"), and past the rate limit (429).

> [!WARNING]
> `rate_limit` does nothing when its `key_path` does not resolve. `get_nested_value` understands only `body.`, `profile.` and `headers.` paths. d37dcc8a added an org-level limit for this reason: `body.bridge_id` is absent on the direct route. The public chatbot limit `state.profile.limiter_key` never applies for the same reason ([agent-configuration](modules/agent-configuration.md#gotchas)).

## Add a built-in tool (or a new tool kind)

Example commits: cdd31179 "feat: add Gtwy_Browser, a built-in tool that drives a real browser (#1744)", and 5212c3d3 "feat: mcp support (#1546)" for a tool kind that is not built in. Background: [tool-calling](modules/tool-calling.md#changing-it-safely), [gtwy-browser](modules/gtwy-browser.md).

1. Add the type name to `inbuild_tools` (or `tool_types`) in `src/configs/constant.py`. Agents enable a built-in tool by listing this exact string in `built_in_tools`.
2. Add an `add_*_tool` helper in `src/services/utils/getConfiguration_utils.py`, next to `add_browser_tool`. It appends the gateway tool shape `{"type": "function", "name", "description", "properties", "required"}` to `tools`. It also writes `tool_id_and_name_mapping[name] = {"type": name, "name": name}`. Do not write provider tool formats yourself; `tool_call_formatter` converts the shape for each provider.
3. Call the helper in `src/services/utils/getConfiguration.py:_prepare_configuration_response`, next to the `add_web_crawling_tool` and `add_browser_tool` calls.
4. Write the executor under `src/services/utils/built_in_tools/`. It must never raise, and it always returns `{"response": ..., "metadata": {"type": ...}, "status": 0|1}`. See `src/services/utils/built_in_tools/firecrawl.py:call_firecrawl_scrape`.
5. Add an `elif` for the type in `src/services/commonServices/baseService/utils.py:process_data_and_run_tools`. If calls must not overlap, add the type to the sequential branch in `_run_tool_tasks`, as Gtwy_Browser does.
6. New settings go in `config.py` and `.env.example` (see [Add an env var](#add-an-env-var)).
7. gtwy-node lists built-in tools for the UI in `getAllInBuiltToolsController` (src/controllers/apiCall.controller.js there). Add an entry, or users cannot switch the tool on. <!-- verify:ignore -->
8. Check it works: put the name in an agent's `built_in_tools`, then call `POST /api/v2/model/chat/completion` with a prompt that needs the tool, once without and once with `stream`. Look for the tool in `response.data.tools_data` and for SSE `tool_call` / `tool_result` events.

> [!WARNING]
> The last `else` in `process_data_and_run_tools` is `axios_work`, the HTTP tool runner. Without step 5, the tool call goes out as an HTTP request with no URL and the model gets an error back. No exception is raised.

## Carry a new agent or request field to the provider

Example commits: 6c6f2d72 "feat: add thread_flag to service parameters and history data" and 0bba141c "feat: add bridge_summary to request and configuration responses". Background: [agent-configuration](modules/agent-configuration.md#changing-it-safely), [chat-orchestration](modules/chat-orchestration.md#changing-it-safely).

The field travels: Mongo agent doc → `get_bridges_with_tools_and_apikeys` → `_prepare_configuration_response` (`base_config`) → `add_configuration_data_to_body` (`body.update(primary_config)`) → `parse_request_body` → `build_service_params` → `BaseService.__init__`.

1. **From the agent doc.** gtwy-node owns the Mongo schema, so add the field there. If a version should inherit it from its parent agent, add it to the parent-lookup `$addFields` stage in `src/db_services/ConfigurationServices.py:get_bridges_with_tools_and_apikeys`. `bridge_summary` is inherited this way.
2. **From the request.** `CompletionRequest` allows extra keys, but declare the field in `src/schemas/completion_schemas.py` if it needs validation. `body.update(primary_config)` runs after the request is read, so the agent value overwrites a request value with the same name. Put the request value back after that line, as is done for `wrapper_id`.
3. Put the field on `base_config` in `src/services/utils/getConfiguration.py:_prepare_configuration_response`. Connected agents go through the same function, so they get it too.
4. Read it in `src/services/utils/common_utils.py:parse_request_body`.
5. Pass it in `build_service_params`, and in `build_service_params_for_batch` if batch needs it. 6c6f2d72 changed both.
6. Read it in `BaseService.__init__` (`src/services/commonServices/baseService/baseService.py`).
7. If the field should be saved with history, add it to the log-queue payload (`make_request_data_and_publish_sub_queue` or `src/db_services/metrics_service.py:build_history_and_metrics_payload`). gtwy-node's log consumer must also read it. Change both repos together.
8. Check it works: delete the agent's `cd_bridge_data_with_tools_` Redis key, or wait for a tag purge. Otherwise the cached agent does not have the field yet. Then call `POST /api/v2/model/chat/completion` and log the value in `BaseService`.

## Add a step after the model answers (response tail)

Example commits: 07dcf9bc "Add folder-level post_tool support with AI response override capability (#1476)", b137e325 "Add reviewer agent functionality to common services (#1479)" and 770dd3c4. Background: [chat-orchestration](modules/chat-orchestration.md), [flow](flows/chat-completion.md).

1. Add the step to `src/services/commonServices/common.py:chat` (the non-stream path).
2. Add the same step to `src/services/utils/common_utils.py:sse_stream_and_finalize` (the stream path). If it applies to batch, also add it to `batch` in `src/services/commonServices/common.py`. 770dd3c4 had to touch all three.
3. Order matters. The reviewer loop runs before post_tool, and post_tool runs before `sendResponse`, so the client gets the modified content (07dcf9bc).
4. If the stream should show progress, add an `emit_*` method on `StreamingService` (`src/services/commonServices/streaming_service.py`), like `emit_review_phase`. Keep `done` last.
5. If the step is configured per agent, carry its settings with the previous recipe.
6. Check it works: run the same agent once without and once with `stream`. Compare the final content, `usage.cost` and the saved history row. Then repeat with `settings.fall_back` enabled and a forced provider error.

> [!WARNING]
> `chat` and `sse_stream_and_finalize` are parallel copies with no shared tail. A step added to only one of them works in the playground and silently does nothing for streamed callers (or the other way round).

## Add an env var

Example commits: c425fd7b "feat: metrics queue (#1501)" and cdd31179 (the `STEEL_*` vars). The catalogue is [environment](environment.md).

1. Add `NAME = os.getenv("NAME")` to `config.py:Config`. For a number, cast with a default: `STEEL_MAX_TABS_PER_HOST = int(os.getenv(...) or 3)`. A bare `int(Config.X)` raises at import when the var is unset.
2. Read it everywhere through `Config`. No file under `src/` calls `os.getenv`; keep it that way.
3. Add it to `.env.example` with a comment that says what it does and what happens when it is missing.
4. Add it to [environment](environment.md).
5. Set it in each deployed environment (dev on `testing`, prod on `master`). This step is outside the repo.
6. Check it works: print the value with `python -c` (import `Config` from `config`). Then run the feature with the var unset and confirm it fails loudly or turns off on purpose.

> [!WARNING]
> A misspelt name is `None`, and the feature turns off with no error. 62c62ad0 renamed `NOTDIAMOND_API_KEY` to `NOT_DIAMOND_API_KEY` to match the deployed name. Until then, the auto model router had silently done nothing. 49263125 fixed a `Config.ENVIROMENT` typo in the new metrics queue. <!-- verify:ignore -->

## Add a Redis key

Example commits: 6008933f "redis prefix added to deelte and not delete (#1579)" and d37dcc8a (`direct_agent_`). Key catalogue and naming rules: [data-model](data-model.md#redis). Helpers: [queues-history-metrics](modules/queues-history-metrics.md#5-redis-helpers-srcservicescache_servicepy).

1. Add the key to `redis_keys` in `src/configs/constant.py`. Prefix the value with `cd_` if it can be rebuilt from the DB and is safe to bulk-delete. Prefix it with `nd_` if Redis is the only copy or it accumulates cost or metrics. gtwy-node's bulk clear skips `nd_` keys. Add the key to the catalogue in data-model.
2. Build the key as `f"{redis_keys['your_key_']}{id}"` and go through the helpers in `src/services/cache_service.py` (`store_in_cache`, `find_in_cache`, ...). They add `REDIS_PREFIX`. The default TTL is `DEFAULT_REDIS_TTL` (2 days). `find_in_cache` returns raw JSON text, so `json.loads` it.
3. If gtwy-node reads, writes or deletes the key too, add the identical string to its src/configs/constant.js.
4. If the value caches Mongo data that gtwy-node edits, store it with `store_in_cache_with_tags` and add the tags in `src/services/cache_utils.py:extract_cache_tags`, so Node's tag purge clears it (4788919c). A new tag kind also needs an entry in `tag_keys` and in Node's tag map.
5. Check it works: run `redis-cli --scan --pattern 'AIMIDDLEWARE_<ENV>_<prefix>*'` after the call, then check the TTL with `TTL <key>`.

> [!WARNING]
> Never write a key as a string literal. After 6008933f renamed `batch_` to `nd_batch_`, `batch_script.py` still scanned `"batch_"`, and batches were never collected until a268cecc. When a key's format changes, change every writer and every purger. 6154fd29 added a `version_` / `bridge_` part to `bridge_data_with_tools_` keys, because versions and agents share `_id` values, and had to update the purge in `update_and_check_cost.py` too. <!-- verify:ignore -->

## Add an alert type

Example commits: 16eb360b "improved alerts (#1400)" and daed8677 "add retry alert type (#1429)". Background: [queues-history-metrics](modules/queues-history-metrics.md#7-alerts).

1. Add the type to `alert_types` in `src/configs/constant.py`.
2. Add a payload builder in `src/utils/alert_template.py`, then map it in `src/utils/alert_utils.py:get_details_payload`. An unmapped type silently falls back to `create_error_payload`.
3. If every org should get it by default, add it to `DEFAULT_ALERT_TYPES`.
4. Raise it with `send_alert(..., error_type=alert_types[...], is_external_error=True)` (`src/send_alert.py`). A webhook only receives the types listed in its `alertType`. For an internal alert that should go to a different flow, add the URL to `alert_webhook` in `src/configs/constant.py` and pass `send_alert(..., webhook_url=alert_webhook[...])`, as `BaseQueue.publish_message` does for RabbitMQ failures.
5. In gtwy-node, add the value to the `alertType` enum in src/mongoModel/Alerting.model.js and to the Joi lists in src/validation/joi_validation/alerting.validation.js. Without that, users cannot subscribe to it. <!-- verify:ignore -->
6. Check it works: point an org's alert webhook at a request bin, trigger the condition, and check the `details.alert_type` of the payload.

## Add a Mongo query or collection

Example commits: 3a4e05e2 "Add per-service timeouts with custom timeout error handling (#1631)" and cdd31179 (`src/db_services/browserCookieService.py`, a collection this repo owns).

1. Put it in a module under `src/db_services/`. Bind the collection at module level, `x_model = db["collection"]`, with `db` from `models/mongo_connection.py`.
2. Wrap every await in `with_timeout(...)` from `src/services/utils/time.py`. That gives a 60 s cap (`SERVICE_TIMEOUTS`). `src/handler/executionHandler.py:handle_exceptions` turns a timeout into a 400 with `TIMEOUT_ERROR_MESSAGE`.
3. Collections that gtwy-node writes get their schema and indexes there. If this repo owns the collection, create its indexes at startup in the `index.py` lifespan (see `src/db_services/browserCookieService.py:ensure_indexes`).
4. Add the collection to [data-model](data-model.md).
5. Check it works: call the code path once with Mongo up. Then block the Mongo host and confirm the caller gets the timeout message instead of hanging.
