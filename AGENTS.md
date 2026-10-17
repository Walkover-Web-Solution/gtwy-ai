# gtwy-ai (AI-middleware-python)

The execution service of GTWY. It is a FastAPI app that takes a completion request for an agent, loads the agent's config from Mongo, calls the LLM provider (with tools, fallback, streaming and billing), and returns the answer.

Its sibling gtwy-node (repo `AI-middleware`) owns agent CRUD, the Mongo schemas, Lago writes and cache invalidation, and it consumes this repo's queues to write history and metrics.

## Commands
| Task | Command |
|---|---|
| Install | `pip install -r req.txt` (Python 3.11) |
| Run locally | `python index.py`, or `uvicorn index:app --port 8080`. It needs Mongo (TLS), Redis, RabbitMQ and a reachable Postgres; setup and required env vars: [docs/environment.md](docs/environment.md#run-it-locally) |
| Check it is up | `curl localhost:8080/healthcheck` |
| Lint / format | `pre-commit run --all-files` (ruff + ruff-format) |
| Test | No test suite. Exercise the changed route by hand and watch the logs. |

Commit messages are checked by commitizen: `type(scope): subject`, and the scope cannot contain `-`. Pushing to `testing` deploys DEV; pushing to `master` deploys PROD. A change usually ships as two PRs, one into `testing` and one into `master`. Keep them identical: 931fd0b3 had to repair master after the two copies drifted.

## Read before you change
- Anything: [docs/architecture.md](docs/architecture.md), then the doc for your area in [docs/README.md](docs/README.md).
- The main request path: [docs/flows/chat-completion.md](docs/flows/chat-completion.md).
- Request auth, agent config, rate limits: [docs/modules/agent-configuration.md](docs/modules/agent-configuration.md).
- `common.py` / `common_utils.py`: [docs/modules/chat-orchestration.md](docs/modules/chat-orchestration.md).
- Providers, models, cost: [docs/modules/llm-providers.md](docs/modules/llm-providers.md). Tools and MCP: [docs/modules/tool-calling.md](docs/modules/tool-calling.md).
- Anything that touches money or limits: [docs/modules/billing-and-limits.md](docs/modules/billing-and-limits.md).
- Adding a provider, route, tool, env var or Redis key: [docs/recipes.md](docs/recipes.md).
- Mongo, Postgres, Redis keys, queues: [docs/data-model.md](docs/data-model.md). Env vars: [docs/environment.md](docs/environment.md). Routes: [docs/api.md](docs/api.md).
- Words like bridge, wallet, `cd_`/`nd_`: [docs/glossary.md](docs/glossary.md).

## Rules
1. **Bridge = agent.** The config the middleware merges into the request body is the source of truth. Per-request overrides go into `bridge_configurations[agent_id]`, never into `body.configuration`, because `chat_multiple_agents` overwrites it. See [agent-configuration](docs/modules/agent-configuration.md).
2. **The response tail exists twice:** in `chat` (non-stream) and `sse_stream_and_finalize` (stream). Any change to fallback, cost, reviewer, post-tool, history or sending the response goes into both. `batch` repeats the history/metrics/cost steps but has no fallback or reviewer, so check it too. See [conventions](docs/conventions.md#layer-rules).
3. **A credit hold is released exactly once, by the frame that popped `credit_hold_token`.**
   - Streaming uses `release_credits_after`.
   - Strip the token from copied or child bodies.
   - In middleware after the hold, raise `HTTPException` so the hold is released.

   See [billing](docs/modules/billing-and-limits.md).
4. **Providers come from the Mongo `services` collection.** Branch on registry predicates (`uses_openai_sdk`, …), not on service names. A new provider still needs `service_name`, `_SERVICE_NAMES` and the list in `update_model_response`. See [recipes](docs/recipes.md).
5. **This repo never writes history or metrics rows.** It publishes to the log and metrics queues; gtwy-node writes them. A new history field must also be mapped in gtwy-node. Call `make_json_serializable` before `publish_message`.
6. **Redis goes through `src/services/cache_service.py`,** which adds the `AIMIDDLEWARE_<ENV>_` prefix.
   - New key prefixes go in `redis_keys` as `cd_` (rebuildable) or `nd_` (must survive a bulk clear).
   - A new entity joined into the agent config needs a cache tag that matches gtwy-node's tagKeys.js.
7. **The registries are imported by reference.** Refresh them with `.clear()` + `.update()`; never rebind the name. Keep the lazy imports listed in [conventions](docs/conventions.md#imports-and-cycles) lazy.
8. **Functions wrapped by `handle_exceptions` return a 400 `JSONResponse`; they do not raise.** Callers of `chat` / `chat_multiple_agents` must check the return value.
9. **Tool executors always return `{"response", "metadata": {"type"}, "status"}`.** The `tools_call_data` keys are read by gtwy-node analytics, so do not rename them.
10. **Gtwy_Browser: never call `browser.close()`, never release a Steel session, and create one only when none is running.** Each of these restarts or tears down Chrome and logs out every conversation on that host. See [gtwy-browser](docs/modules/gtwy-browser.md).
11. **Read every env var through `config.py:Config`.** Never log `settings` or configs: they hold decrypted API keys (e.g. `settings.fall_back.apikey`).
12. **Run locally with `CONSUMER_STATUS=false`, or with private queue names.** Otherwise your laptop consumes shared jobs.
13. **Do not "fix" load-bearing misspellings** (`Encreaption_key`, `last_transffered_agent_`, the `MIDDLEARE` queue name, …). The full list is in [conventions](docs/conventions.md#load-bearing-odd-spellings).
14. **Any value shared with gtwy-node must change in both repos together:** queue names, Redis keys, cache tags and billing constants. See [architecture](docs/architecture.md#cross-repo-contracts).
15. **When a change alters behaviour a doc describes, update that doc in the same commit,** then run `/ai-docs update`.

## Where things are
- `index.py`: app, router mounts, startup and shutdown.
- `config.py`: every env var.
- `src/configs/`: constants and the Mongo-fed registries.
- `src/routes/`, `src/schemas/`, `src/middlewares/`: the HTTP surface, auth and agent-config assembly.
- `src/services/commonServices/common.py` and `src/services/utils/common_utils.py`: completion orchestration. These change most often.
- `src/services/commonServices/baseService/`: provider dispatch and the tool loop. Provider folders sit next to it.
- `src/services/billing/`, `src/services/todo/`, `src/services/mcp_gateway/`, `src/services/utils/built_in_tools/`: billing, plan mode, MCP, built-in tools.
- `src/db_services/`, `models/`: Mongo, Postgres and Timescale access. The schemas themselves live in gtwy-node.

## Tools
The repo configures the code-review-graph MCP server (`.mcp.json`). When it is connected, use its graph tools first: `semantic_search_nodes` / `query_graph` to explore, `get_impact_radius` / `get_affected_flows` for the blast radius, and `detect_changes` / `get_review_context` for reviews. <!-- verify:ignore -->
It is optional: the `code-review-graph` binary must be on PATH. Without it, use grep and these docs. Nothing here depends on it.

## Docs map
[docs/README.md](docs/README.md) lists every doc, when to read it, and the open questions that need a human.
These docs are checked against the code: `/ai-docs check` shows what has drifted, and `/ai-docs update` fixes it.
