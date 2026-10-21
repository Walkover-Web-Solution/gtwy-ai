---
covers: ["config.py", ".env.example", "Dockerfile", "req.txt"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Environment

Every variable is read once, in `config.py:Config`, right after `load_dotenv(override=True)`. No other file calls `os.getenv`; code reads `Config.<NAME>`. `.env.example` lists most of the names (copy it to the .env file); a few only appear in `config.py`.

> [!WARNING]
> `load_dotenv(override=True)` means a value in the .env file beats the same variable exported in your shell or set on the command line. To change a value for one run, edit the .env file (or a worktree copy of it), not the shell.

## Run it locally

1. Python 3.11. The image is `python:3.11` (`Dockerfile`) and `ruff.toml` targets `py311`.
2. Install: `python3.11 -m venv venv && . venv/bin/activate && pip install -r req.txt`.
3. `cp .env.example .env` and fill it. These must be set or the process will not start (see the table for why): `REDIS_URI`, `MONGODB_DATABASE_NAME`, `TIMESCALE_SERVICE_URL`, `max_workers`, `CONSUMER_STATUS`, and `DB_*` pointing at a reachable Postgres. With `LAGO_API_URL` set, `LAGO_CREDIT_RATE_USD` is also mandatory.
4. Services it connects to. They are the same instances gtwy-node uses ([must match](#must-match-other-services)):
   - MongoDB. `models/mongo_connection.py` always connects with `tls=True` and the `certifi` CA bundle, so a plain local `mongod` without TLS will not work; use Atlas or a TLS-enabled server. The registry listeners in `src/configs/` use change streams, which need a replica set.
   - Redis (`REDIS_URI`).
   - RabbitMQ (`QUEUE_CONNECTIONURL`).
   - Postgres for conversation history (`DB_*`, `models/postgres/pg_connection.py`).
   - Timescale/Postgres for metrics (`TIMESCALE_SERVICE_URL`, `models/Timescale/connections.py`).
5. Start one of:
   - `python index.py`: uvicorn on `0.0.0.0:$PORT` (default 8080), see the `__main__` block in `index.py`.
   - `uvicorn index:app --port 8080`.
   - Production (`Dockerfile` CMD): `opentelemetry-instrument gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080 --timeout 300 index:app`. It binds 8080 and ignores `PORT`.
6. Check it is up: `curl localhost:8080/healthcheck` gives `{"status": "OK running good... v1.3"}`, and `GET /ready` gives 200 (routes in [api](api.md#app-routes)). The startup log should show `Model configurations refreshed successfully.`, `Service registry refreshed successfully (N services).`, `Billing plans refreshed successfully (...)` and a `[billing] credit rate` line. `platform_apikeys collection is EMPTY` means wallet-billed agents have no provider keys.

> [!WARNING]
> `CONSUMER_STATUS=true` makes this process consume the main queue named by `QUEUE_NAME` and run the batch-result cron ([queues](modules/queues-history-metrics.md#2-what-consumer_status-changes)). Pointed at a shared environment, a local server then takes other people's jobs. Locally, set it to `false`, or give `QUEUE_NAME`, `LOG_QUEUE_NAME` and `METRICS_QUEUE_NAME` private values.

> [!WARNING]
> `README.md` says `uvicorn app.main:app --reload`. There is no app package; the app is `index:app`.

Startup does not fail when Mongo or RabbitMQ are down, but it does fail when the history Postgres is unreachable: `models/postgres/pg_connection.py` runs `metadata.reflect(bind=engine)` at import time with no `try`, and the chat path imports it through `src/db_services/conversationDbService.py`. If Mongo is unreachable the loaders in `src/configs/` log errors and stay empty, and every completion then fails with `"model or service does not exist!"`. If RabbitMQ is unreachable, queued responses fail with a 500. When `ENVIRONMENT` is `LOCAL`, the Timescale connection check runs in the background instead of blocking startup (`index.py:lifespan`).

## Lint and commit tooling

- `pre-commit install && pre-commit install --hook-type commit-msg`. `.pre-commit-config.yaml` runs `ruff --fix`, `ruff-format`, whitespace/EOF/YAML/large-file/merge-conflict checks, and commitizen on the commit message.
- `ruff.toml`: line length 120, target py311, rules `E W F I B C4 UP`, ignores `E501 B008 C901`. Run by hand with `ruff check --fix . && ruff format .`. Ruff does not read `.ruffignore`; exclusions belong in `ruff.toml`.
- Commit messages must match `.cz.toml` `schema_pattern`: `type(scope): subject` with type in feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert. The scope is `\w+`, so a hyphen fails the hook: `fix(gtwy-browser): …` is rejected, `fix(browser): …` passes.
- `.cz.toml` bumps the version in pyproject.toml, which this repo does not have, so `cz bump` fails. <!-- verify:ignore -->
- There is no lint or test CI. `.github/workflows/alert.yml` only calls a deploy webhook on pushes to `testing` (DEV) and `master` (PROD).

## Variables

Required: **crash** = the process fails at import or startup; **yes** = core requests fail; **feature** = only that feature breaks; **no** = has a fallback. "Used by" is the code that reads the `Config` attribute.

### Core

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `ENVIRONMENT` | yes | — | `src/services/cache_service.py:REDIS_PREFIX`, `src/configs/blocked_orgs.py:BLOCKED_ORGS_KEY`, queue default names, alerts | Every Redis key starts `AIMIDDLEWARE_{ENVIRONMENT}_`. Unset puts the literal text None in the prefix, so this process stops sharing keys (blocked orgs, rate limits, billing holds, caches) with gtwy-node. `LOCAL` also stops the Timescale check from blocking startup. |
| `PORT` | no | 8080 | `index.py` `__main__` | Port for `python index.py` only. |
| `max_workers` | crash | — | `src/routes/v2/modelRouter.py:executor`, `src/services/commonServices/baseService/baseService.py:executor` | Thread pool size. Both modules run `int(Config.max_workers) or 10` at import; unset is `int(None)`, a `TypeError`. The `or 10` never applies. Lowercase name. |
| `CONSUMER_STATUS` | crash | — | `index.py:lifespan`, `src/services/grafana.py` | `"true"` (any case) starts the main-queue consumer and the batch cron. Unset raises `AttributeError` on `.lower()` during startup. |

### Datastores

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `MONGODB_CONNECTION_URI` | yes | — | `models/mongo_connection.py` | Mongo URI (TLS forced). Unset points the client at localhost; registries load empty. |
| `MONGODB_DATABASE_NAME` | crash | — | `models/mongo_connection.py` | Database name. Unset raises `TypeError` at import. |
| `REDIS_URI` | crash | — | `src/services/cache_service.py:client` | A redis:// or rediss:// URL. Unset raises `ValueError` at import. |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASS` | crash | — | `models/postgres/pg_connection.py` | History Postgres, URL `postgresql+psycopg2://USER:PASS@HOST/NAME`. Must point at a reachable Postgres: `metadata.reflect(bind=engine)` runs at import with no `try`, so an unset or unreachable database stops the process from starting (see [data-model.md](data-model.md)). |
| `TIMESCALE_SERVICE_URL` | crash | — | `models/Timescale/connections.py` | A postgresql:// URL; the code rewrites it to postgresql+asyncpg:// and strips `?sslmode=require`. Unset raises `AttributeError` at import. Metrics reads use it (`src/db_services/metrics_service.py`). |

### Queues (RabbitMQ)

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `QUEUE_CONNECTIONURL` | yes | — | `src/services/commonServices/queueService/baseQueue.py:ConnectionManager` | AMQP URL. Without it, connect errors are logged, queued responses return 500 and history/metrics are never published. |
| `QUEUE_NAME` | no | `AI-MIDDLEARE-DEFAULT-{ENVIRONMENT}` | `src/services/commonServices/queueService/queueService.py` | Main queue (plus `<name>-Failed`). The misspelt default is in the code. |
| `LOG_QUEUE_NAME` | no | `AI-MIDDLEARE-DATA-QUEUE-{ENVIRONMENT}` | `src/services/commonServices/queueService/queueLogService.py` | History/billing log queue. gtwy-node consumes it, so the name must match Node's. |
| `METRICS_QUEUE_NAME` | no | `AI-MIDDLEWARE-METRICS-QUEUE-{ENVIRONMENT}` | `src/services/commonServices/queueService/queueMetricsService.py` | Metrics queue, consumed by gtwy-node. Must match. |
| `PREFETCH_COUNT` | no | 50 | `src/services/commonServices/queueService/baseQueue.py` | Consumer QoS prefetch. |

Queue behaviour: [queues-history-metrics](modules/queues-history-metrics.md).

### Auth and secrets

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `SecretKey` | yes | — | `src/middlewares/middleware.py:jwt_middleware`, `src/routes/v2/browserRouter.py:live_view`, `src/services/utils/built_in_tools/browser/steel_client.py:permanent_live_url` | HS256 key for user JWTs and browser live links. Wrong value: every JWT call is a 401. |
| `CHATBOTSECRETKEY` | feature | — | `src/middlewares/interfaceMiddlewares.py:chat_bot_auth` | Verifies chatbot tokens (gtwy-node signs them). |
| `public_chatbot_token` | feature | — | `chat_bot_auth`, `src/middlewares/agentsMiddlewares.py:agents_auth` | Verifies public-agent tokens. Lowercase env name; the attribute is `Config.PUBLIC_CHATBOT_TOKEN`. |
| `Encreaption_key` | yes | — | `src/services/utils/helper.py:Helper`, `src/services/utils/built_in_tools/browser/cookies.py` | AES key (sha512-derived) that decrypts provider API keys stored by gtwy-node and encrypts saved browser logins. Unset or wrong: every stored key fails to decrypt. |
| `Secret_IV` | yes | — | same | The AES IV, same derivation. |
| `ADMIN_API_KEY` | feature | — | `src/services/proxy/Proxyservice.py`, `src/services/proxy/embed_service.py` | MSG91 admin key for `pauthkey` / `proxy_auth_token` auth, org lookups, and embed-token user creation (`getCompanies`, `createCUsers`). |
| `PUBLIC_REFERENCEID` | feature | — | `src/services/proxy/Proxyservice.py`, `src/services/proxy/embed_service.py` | MSG91 reference id in the `getCompanies` / `getDetails` URLs, and the `feature_id` when an embed user is created. |
| `PROXY_USER_ROLE_ID` | feature | — | `src/services/proxy/embed_service.py` | MSG91 role given to users auto-created for gtwy embed tokens. Unset sends no role; embed sign-in may fail. Must match gtwy-node's value. |
| `AI_MIDDLEWARE_PAUTH_KEY` | feature | — | `src/services/utils/ai_call_util.py` | `pauthkey` for calls to gtwy's own platform agents (query refiner, prompt helpers). Unset: those calls fail. |

> [!WARNING]
> `Encreaption_key` and `Secret_IV` are misspelt on purpose: gtwy-node reads the same names. Do not rename them. Changing their values makes every stored API key and saved browser login undecryptable.

> [!WARNING]
> `src/services/utils/ai_call_util.py` sends `AI_MIDDLEWARE_PAUTH_KEY` to the hard-coded `https://api.gtwy.ai`, whatever `ENVIRONMENT` says. A local or testing server calls production with it.

### Billing (Lago)

Behaviour and the full cross-repo list: [billing-and-limits](modules/billing-and-limits.md#cross-repo-contracts).

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `LAGO_API_URL` | no | — | `src/services/billing/lago_service.py`, `src/services/billing/billing_utils.py` | Lago base URL. Unset turns wallet billing off. |
| `LAGO_API_KEY` | with `LAGO_API_URL` | — | `src/services/billing/lago_service.py` | Bearer token for Lago. |
| `LAGO_CREDIT_RATE_USD` | crash if `LAGO_API_URL` is set | — | `src/services/billing/billing_utils.py:_load_credit_rate` | USD per credit. Missing or not positive while `LAGO_API_URL` is set raises `RuntimeError` at import. Must match Node. |
| `LAGO_PLAN_CODE_FREE`, `LAGO_PLAN_CODE_PAID` | yes with Lago | — | `src/services/billing/lago_service.py:_PLAN_CODE_TO_SLUG` | Lago plan codes for the `free` / `paid` slugs. An unmapped plan is treated as `free`. Must match Node. |
| `GTWY_PLATFORM_ORG_ID` | no | — | `src/services/billing/billing_utils.py:reserve_credits_and_api_key_setup` | Org whose own traffic is never wallet-billed or gated. Must match Node. |
| `GTWY_COMMISSION_PCT` | no | 0 | `src/services/billing/billing_utils.py:_load_commission_pct` | Percent added to provider cost. Non-numeric or outside 0–100 raises `RuntimeError` at import. Must match Node. |

### Realtime

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `RTLAYER_AUTH` | feature | — | `src/routes/v2/modelRouter.py:chat_completion`, `src/middlewares/interfaceMiddlewares.py:send_data_middleware`, `src/services/testcase_service.py:build_rtlayer_cred`, `src/services/utils/common_utils.py`, the change listeners in `src/configs/` | RTLayer key for playground and chatbot answers, testcase events, and model/service/plan update broadcasts. Unset: those pushes fail. |

### Integrations

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `GCP_CREDENTIALS` | feature | — | `src/services/utils/gcp_upload_service.py:uploadDoc` | Service-account JSON as one string. Unset: every non-video upload fails. The bucket name is hard-coded. |
| `HIPPOCAMPUS_API_KEY` | feature | — | `src/controllers/rag_controller.py`, `src/services/commonServices/response_caching_utils.py` | RAG search and the response cache. Unset: `/rag/query` fails; the cache lookup is skipped. |
| `HIPPOCAMPUS_COLLECTION_ID` | no | — | `src/services/commonServices/response_caching_utils.py` | Response-cache collection. Unset: cache skipped. |
| `FIRECRAWL_API_KEY` | no | — | `src/services/utils/built_in_tools/firecrawl.py` | Web crawl tool. Unset: the tool returns "web_crawling tool is not configured". |
| `NOT_DIAMOND_API_KEY` | no | — | `src/services/auto_router_service.py`, `src/services/utils/auto_router_utils.py` | Auto model router. Unset: auto model selection does nothing. |
| `OPENAI_API_KEY_GPT_5_NANO` | no | — | `src/services/utils/getConfiguration_utils.py` | Fallback key for chatbot agents on `gpt-5-nano` with no key of their own. |
| `OPENAI_API_KEY` | no | — | `src/services/utils/guardrails_validator.py:guardrails_check` | Guardrails only, and guardrails are not wired in ([reviewer-and-guardrails](modules/reviewer-and-guardrails.md)). |
| `CHATBOT_OPTIONS_TEMPLATE_ID` | feature | — | `src/services/utils/common_utils.py` | Template id for chatbot answer suggestions. |
| `STEEL_API_URLS` | no | — | `src/services/utils/built_in_tools/browser/steel_client.py:hosts` | Comma-separated Steel hosts for Gtwy_Browser. Wins over `STEEL_API_URL`. Neither set: the tool, its reaper and its indexes are off. |
| `STEEL_API_URL` | no | — | same | Single Steel host, used when `STEEL_API_URLS` is empty. |
| `STEEL_MAX_TABS_PER_HOST` | no | 3 | `config.py`, `src/services/utils/built_in_tools/browser/session_store.py` | Conversations per Steel Chrome. Parsed with `int()` at import, so a non-number crashes startup. |
| `GTWY_PUBLIC_URL` | no | — | `src/services/utils/built_in_tools/browser/steel_client.py:permanent_live_url` | Public base URL for `/browser/live/{token}` links. Unset: users get the direct Steel link, which dies with the tab. See [gtwy-browser](modules/gtwy-browser.md). |

### Observability

| Var | Required | Default | Used by | Purpose / effect if missing |
|---|---|---|---|---|
| `OTEL_SERVICE_NAME` | no | — | `src/services/grafana.py` | Pyroscope application name and tag. |
| `PROFILES_SERVER_ADDRESS` | no | — | `src/services/grafana.py` | Pyroscope server. If `pyroscope.configure` fails the error is logged and the app runs. |
| `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_ENDPOINT` | no | — | none in app code | `Config` reads them but nothing uses the attributes. `opentelemetry-instrument` (Docker CMD) reads `OTEL_*` from the process environment when it starts, before `config.py` loads the .env file, so set them in the container environment, not only in the .env file. |

### Read but unused

`config.py` reads these and no code uses them at af2a2886. Leave them empty; remove them from `config.py` and `.env.example` together if you clean up.

`AI_ML_APIKEY`, `ALERTING_PROJECT_ID`, `ALGORITHM` (JWT algorithms are hard-coded `HS256`), `Access_key`, `Chatbot_Access_key` (attribute `Config.CHATBOT_ACCESS_KEY`), `DOCSTAR_ACCESS_KEY`, `DOCSTAR_ORG_ID`, `DOCSTAR_COLLECTION_ID`, `JWT_TOKEN_SECRET` (user JWTs use `SecretKey`), `MONGODB_USER_DATABASE`, `MONGODB_USER_DATABASE_COLLECTION`, `OPTIONS_APIKEY`, `ORG_ID`, `PROJECT_ID`, `PINECONE_API_KEY` (attribute `Config.PINECONE_APIKEY`), `PINECONE_INDEX`, `TEMPLATE_ID`, `TRIGGER_PROJECT_ID` (default `""`).

`.env.example` also has `URL`, which nothing reads.

## Must match other services

gtwy-node (the sibling repo, `/Users/husainbaghwala/walkover/AI-middleware`) shares the databases, Redis, queues and secrets. A mismatch fails silently or as auth errors, so change these in both deployments together.

<!-- verify:ignore-start -->
| This repo | gtwy-node name | What breaks if they differ |
|---|---|---|
| `SecretKey` | `SecretKey` | Node signs user JWTs; every call from the UI is a 401 here. |
| `CHATBOTSECRETKEY` | `CHATBOTSECRETKEY` | Node signs chatbot tokens (`src/services/utils/users.service.js`); chatbot calls fail. |
| `public_chatbot_token` | `PUBLIC_CHATBOT_TOKEN` | Different spelling, same secret. Node signs public-agent tokens with it. |
| `Encreaption_key`, `Secret_IV` | same names | Node encrypts provider keys and platform keys; Python cannot decrypt them. |
| `ENVIRONMENT` | `ENVIRONMENT` | Redis prefix `AIMIDDLEWARE_{ENVIRONMENT}_`: blocked orgs, rate limits, billing balances and cache invalidation stop being shared. |
| `REDIS_URI` | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB` | Must be the same Redis. |
| `MONGODB_CONNECTION_URI`, `MONGODB_DATABASE_NAME` | `MONGODB_URI`, `MONGODB_DB_NAME` | Node owns the Mongo schemas; agents, keys and registries come from there. |
| `QUEUE_CONNECTIONURL` | `RABBITMQ_URL` | Same broker. |
| `LOG_QUEUE_NAME`, `METRICS_QUEUE_NAME` | same names | Node consumes them; history, billing events and metrics are lost if the names differ. |
| `DB_*` | `POSTGRES_*` | History written via Node's log consumer is read back here. |
| `TIMESCALE_SERVICE_URL` | `TIMESCALE_*` | Same metrics database. |
| `LAGO_CREDIT_RATE_USD`, `LAGO_PLAN_CODE_FREE`, `LAGO_PLAN_CODE_PAID`, `GTWY_PLATFORM_ORG_ID`, `GTWY_COMMISSION_PCT` | same names | Charges and plan gates disagree between the two services. |
| `LAGO_API_URL`, `LAGO_API_KEY` | `BILLING_API_URL`, `BILLING_API_KEY` | Same Lago instance, different names. |
<!-- verify:ignore-end -->
