---
covers: ["ruff.toml", ".pre-commit-config.yaml", ".cz.toml", "src/handler/**", "src/services/cache_service.py", "src/configs/**", "src/services/commonServices/queueService/**"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Conventions

Only rules specific to this repo. Each one has caused a bug or a near-miss.

## Where new code goes

| Kind of code | Folder | Example |
|---|---|---|
| Route + request schema | `src/routes/` (`src/routes/v2/` for model routes) + `src/schemas/` | `src/routes/v2/modelRouter.py`, `src/schemas/completion_schemas.py:CompletionRequest` |
| Auth / request-shaping dependency | `src/middlewares/` | `src/middlewares/getDataUsingBridgeId.py` |
| Orchestration step for a completion | `src/services/utils/common_utils.py` (helpers) called from `src/services/commonServices/common.py` | `handle_pre_tools` |
| Provider | `src/services/commonServices/<provider>/` (Call + ModelRun), formatter in `src/services/utils/formatters/` | see [recipes.md](recipes.md) |
| Built-in tool | `src/services/utils/built_in_tools/` | `src/services/utils/built_in_tools/firecrawl.py` |
| Mongo / Postgres access | `src/db_services/` | `src/db_services/ConfigurationServices.py` |
| Env var | `config.py:Config` only (there are no `os.getenv` calls under `src/`) | `Config.REDIS_URI` |
| Redis key prefix | `redis_keys` in `src/configs/constant.py` | `nd_batch_` |
| Background loop | started and cancelled in `index.py:lifespan` | [startup flow](flows/startup-and-background-jobs.md#where-to-add-a-new-background-job) |

## Layer rules

- **Branch on capabilities, not service names.** Use the registry predicates in `src/configs/service_registry.py` (`uses_openai_sdk`, `has_openai_choices_shape`). Name lists drift whenever a provider is added. Some name lists still exist, and new services must be added to them; see [llm-providers.md](modules/llm-providers.md).
- **The response tail exists twice.** Fallback, cost, the reviewer, post-tool, history and sending the response are implemented in both `src/services/commonServices/common.py:chat` (non-stream) and `src/services/utils/common_utils.py:sse_stream_and_finalize` (stream). Change both, or the two paths drift; the stream copy once skipped the plan check on fallback.
- **One owner per credit hold.** Only the frame that pops `credit_hold_token` releases it, exactly once. Streaming hands the release to `release_credits_after`. Anything that copies a request body for a child call must strip the token. ([billing-and-limits.md](modules/billing-and-limits.md))
- **Per-request config overrides go into `bridge_configurations[agent_id]`, never into `body.configuration`.** `chat_multiple_agents` runs `body.update(agent_config)`, which replaces `configuration`.
- **Registries are mutated in place.** `model_config_document`, `service_registry_document` and `plan_registry_document` are imported by reference all over the code. Refresh them with `.clear()` + `.update()`, never by rebinding the name.
- **Python never writes history or metrics rows.** It publishes `save_history` / `save_metrics` messages and gtwy-node writes them. A new history field must also be mapped in gtwy-node's consumer.
- **Every tool executor returns `{"response", "metadata": {"type"}, "status"}`.** Returning `None` crashes the whole tool round ([tool-calling.md](modules/tool-calling.md)).
- **A new joined entity in the agent config needs a cache tag** in `extract_cache_tags` and `tag_keys`, matching gtwy-node. Without one, edits stay invisible for the 2-day cache TTL.

## Errors and logging

- Service functions decorated with `src/handler/executionHandler.py:handle_exceptions` return a `JSONResponse` (400) instead of raising. To control the body, raise `ValueError` with a dict. Callers of `chat` / `chat_multiple_agents` must check for a returned error response; they never get an exception.
- In middlewares that run after the credit hold, raise `HTTPException`: the except branch releases the hold.
- Internal alerts go through `send_alert` (`src/send_alert.py`). The flow URLs live in `alert_webhook` in `src/configs/constant.py`; pass `send_alert(webhook_url=...)` to send to a different flow. A failed chat already alerts twice; don't add a third. In `src/services/commonServices/queueService/baseQueue.py`, import `send_alert` inside the function (circular import).
- Use `logger` from `globals.py`. There are many `print` calls in the code; don't add more.

## Redis and queues

- Go through the helpers in `src/services/cache_service.py` (`store_in_cache`, `find_in_cache`, …) so `REDIS_PREFIX` is applied. `find_in_cache` returns raw JSON text: call `json.loads` on it.
- New key prefixes go in `redis_keys` with `cd_` (safe to delete; rebuilt from a DB) or `nd_` (Redis is the only copy, or a counter). gtwy-node's bulk clear deletes everything that does not start with `nd_`.
- Pass messages through `make_json_serializable` before `publish_message`. A datetime or ObjectId makes all 3 publish attempts fail, and each failed attempt also sends an alert.
- Never rename queue names, including the `AI-MIDDLEARE-DEFAULT-…` typo; queued messages would be orphaned. A new queue must be declared in `index.py:lifespan`, because publishing to an undeclared queue is dropped silently.

## Imports and cycles

These imports are lazy on purpose, because the modules import each other at load time:
- `send_message` inside the registry change listeners
- `Helper` inside `get_platform_apikeys`
- the predicates inside `src/services/utils/token_calculation.py`
- `model_configuration` inside `get_platform_apikey`

Keep them inside the function. If you move one to the top of the file, startup fails with an `ImportError`.

## Load-bearing odd spellings

Do not "fix" these. Other code, gtwy-node or deployed environments depend on them:
- env vars `Encreaption_key` and `Access_key`
- the Redis key `last_transffered_agent_`
- the queue name typo `MIDDLEARE`
- `update_configration`

## Approved libraries

| Concern | Use | Notes |
|---|---|---|
| HTTP calls to tools and flows | `src/services/utils/apiservice.py:fetch` | Retries POSTs on 502/520. `unknown_error_handler_alert` must **not** use it, because that loops. |
| Mongo | Motor (`models/mongo_connection.py:db`) | TLS is always on |
| Postgres / Timescale | SQLAlchemy async (`models/postgres/`, `models/Timescale/`) | |
| Redis | `redis.asyncio` through `src/services/cache_service.py` | |
| Queues | aio-pika through `src/services/commonServices/queueService/baseQueue.py` | |
| Validation | Pydantic models in `src/schemas/` | |

## Before you finish a change

- Run the lint and commit checks in [environment.md](environment.md#lint-and-commit-tooling) (ruff via pre-commit; commitizen commit messages, whose scope may not contain a hyphen).
- There is no automated test suite. Exercise the changed path by hand: a curl against `/api/v2/model/chat/completion` with a test agent, and watch the logs. The "testcases" routes are a product feature, not this repo's tests.
- Pushing to `testing` deploys to DEV and pushing to `master` deploys to PROD.
- If you changed behaviour a doc describes, update that doc in the same change.
