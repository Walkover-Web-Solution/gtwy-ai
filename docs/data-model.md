---
covers: ["models/**", "src/configs/constant.py", "src/db_services/**", "src/services/cache_service.py", "src/services/commonServices/queueService/**"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Data model

Every store this service reads or writes. This repo owns almost no schema: gtwy-node (the sibling Node repo, AI-middleware) defines the Mongo models, runs the Postgres and Timescale migrations, and consumes the log and metrics queues. Python reads config from Mongo, keeps caches and counters in Redis, and publishes history and metrics for gtwy-node to write. Behaviour is in the linked module docs; connection env vars are in [environment](environment.md).

| Store | Used for | Connection | Schema owner |
|---|---|---|---|
| MongoDB | agents, API keys, tools, registries (services, models, plans), test cases, browser cookies | `MONGODB_CONNECTION_URI`, `MONGODB_DATABASE_NAME` → `models/mongo_connection.py:db` (Motor, TLS always on) | gtwy-node, except `gtwy_browser_cookies` |
| Postgres | conversation history (read-only here) | `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASS` → `models/postgres/pg_connection.py:Session` (sync psycopg2) | gtwy-node |
| Timescale | per-call metrics, written by gtwy-node | `TIMESCALE_SERVICE_URL` → `models/Timescale/connections.py:AsyncSession` | gtwy-node |
| Redis | caches, rate limits, spend counters, billing shadow balance, plans, browser sessions, locks | `REDIS_URI` → `src/services/cache_service.py:client` | shared: both repos read and write some keys |
| RabbitMQ | request queue, log (history + side jobs) queue, metrics queue | `QUEUE_CONNECTIONURL` → `src/services/commonServices/queueService/baseQueue.py:ConnectionManager` | this repo publishes; gtwy-node consumes log and metrics |
| GCP Cloud Storage | uploaded files, generated images | `GCP_CREDENTIALS` → `src/services/utils/gcp_upload_service.py:uploadDoc` | this repo |

## MongoDB

`models/mongo_connection.py` builds one Motor client at import. Every module takes a raw handle (`db["configurations"]` and so on), so there are no Python schemas: the fields below are the ones the code reads or writes, checked against the gtwy-node Mongoose model named in the table. Index columns list what gtwy-node declares; this repo declares indexes only on `gtwy_browser_cookies`. Most calls are wrapped in `with_timeout` (`src/services/utils/time.py`, 60 s for Mongo).

| Collection | Purpose | Python reads | Python writes | gtwy-node model · indexes |
|---|---|---|---|---|
| `configurations` | Agents ("bridges"). | `src/db_services/ConfigurationServices.py:get_bridges_with_tools_and_apikeys` (plus `$lookup` of connected agents and the parent agent), `get_bridge_by_slugname`, `get_agents_data` | `src/services/direct_agent_service.py:get_or_create_direct_agent` (upsert of the hidden per-org agent) | Configuration.model.js · unique (org_id, slugName); TTL on deletedAt, 30 days; (org_id, deletedAt) |
| `configuration_versions` | Agent versions; `parent_id` is the agent id. | `get_bridges_with_tools_and_apikeys` (by `version_id`, or via `settings.environment_config.<env>`) | none live (`update_bridge` has no callers) | BridgeVersion.model.js · TTL on deletedAt, 30 days; (org_id, deletedAt) |
| `apicalls` | Tools (HTTP / script tools). | `$lookup` in `get_bridges_with_tools_and_apikeys` by the ids in `connected_tools` (entries of type `tools`, `pre_tool`, `post_tool`), legacy `pre_tools`, and folder `config.tools_id` / `config.pre_tool_id` / `config.post_tool` (or legacy `config.post_tool_id`). Reviewer tools are no longer looked up | none | ApiCall.model.js · (org_id, script_id) |
| `apikeycredentials` | Org provider keys, encrypted (`Helper.decrypt` in `src/services/utils/helper.py`). | `$lookup` in `get_bridges_with_tools_and_apikeys`; `src/db_services/apikey_usage_service.py:get_usage` | `src/db_services/apikey_usage_service.py:add_usage` (spend for the current period); `src/db_services/api_key_status_service.py:update_apikey_status` | Api.model.js · unique (name, org_id, folder_id) |
| `folders` | Folders / embeds: shared keys, tools, prompt, limits, wrapper. | folder pipeline in `get_bridges_with_tools_and_apikeys` | none | GtwyEmbed.model.js (model "Folder") · unique (org_id, name) |
| `templates` | Response templates. | `get_template_by_id` (cached as `template_{id}`) | none | Template.model.js |
| `rich_ui_templates` | Rich UI templates for `is_template` responses. | `$lookup` by `configuration.response_type.value.template_id` → `richui_templates` | none | RichUiTemplate.model.js · (name) |
| `prompt_wrappers` | Prompt wrappers applied around the agent prompt. | `get_prompt_wrapper_by_id` (from `src/services/utils/common_utils.py`) | none | PromptWrapper.model.js |
| `preBuiltPrompts` | Org overrides of built-in prompts, and global prompts (todo mode). | `src/services/prebuilt_prompt_service.py` (see warning) | none | PrebuiltPrompt.model.js, which resolves to `prebuiltprompts` (see warning) |
| `agent_memories` | Response cache answers keyed by a Hippocampus `resource_id`. | `src/services/commonServices/response_caching_utils.py:get_response_using_resourceid` | none (gtwy-node writes from `save_agent_memory`) | inline schema in src/services/logQueue/saveToAgentMemory.service.js |
| `testcases` | Agent test cases. | `src/services/testcase_service.py:fetch_testcases_from_request` | `src/db_services/testcase_services.py:update_testcase_last_executed`; CRUD in `src/controllers/testcase_controller.py` is not routed | Testcase.model.js |
| `alerts` | Org alert webhooks. | `src/db_services/webhook_alert_Dbservice.py:get_webhook_data` (only on the unused external-alert path of `send_alert`) | none | Alerting.model.js · unique (org_id, name) |
| `services` | Provider registry. | `src/services/utils/load_service_configs.py:get_service_configs` (`status: 1`) + change stream in `src/configs/service_registry.py` | none | Service.model.js · unique service_name |
| `modelconfigurations` | Model registry: params, pricing, validation. | `src/services/utils/load_model_configs.py:get_model_configurations` (`status: 1`) + change stream in `src/configs/model_configuration.py` | none | ModelConfig.model.js · unique (model_name, service); TTL on disabled_at, 30 days, only when status 0 |
| `platform_apikeys` | GTWY's own provider keys (wallet billing), encrypted. | `src/services/utils/load_model_configs.py:get_platform_apikeys` + change stream | none | PlatformApiKey.model.js · unique service |
| `billing_plans` | Plan allowlists, credit grants, hit fees. | `src/services/utils/load_plan_configs.py:get_plan_configs` (`status: 1`) + change stream in `src/configs/plan_registry.py` | none | BillingPlan.model.js · unique plan_code |
| `gtwy_browser_cookies` | Encrypted browser cookie jar per conversation. | `src/db_services/browserCookieService.py:get_browser_cookies` | `upsert_browser_cookies` | **this repo**: `ensure_indexes` (run from `index.py`) creates unique `scope_key` and TTL `expires_at` (expireAfterSeconds 0) |

`threads` has a handle (`threadsModel` in `src/db_services/ConfigurationServices.py`) that nothing uses; gtwy-node owns that collection.

### Key fields

| Collection | Fields the Python code relies on |
|---|---|
| `configurations` / `configuration_versions` | `_id`, `org_id`, `name`, `slugName`, `bridgeType`, `service`, `configuration` (model, prompt, `response_type`, `type`…), `settings` (`response_format`, `fall_back`, `guardrails`, `review_agent`, `environment_config`, `publicAgentConfig`), `connected_tools` (typed entries — `tools`, `agent`, `pre_tool`, `post_tool`, docs, built-in tools — that now decide the agent's tools; see [agent-configuration](modules/agent-configuration.md)), legacy `function_ids`, `pre_tools`, `post_tool`, `connected_agents`, `agent_info`, `apikey_object_id` (`{service: apikeycredentials _id}`), `folder_id`, `bridge_limit` / `bridge_usage` / `bridge_limit_reset_period` / `bridge_limit_start_date`, `variables_path`, `doc_ids`, `gpt_memory`, `gpt_memory_context`, `built_in_tools`, `published_version_id`, `parent_id` (versions), `bridge_status`, `deletedAt` |
| `apicalls` | `_id`, `org_id`, `script_id`, `url` (the POST target for the tool and for webhook reviewers), `title`, `description`, `fields`, `bridge_ids` |
| `apikeycredentials` | `_id`, `org_id`, `name`, `service`, `apikey` (encrypted), `folder_id`, `apikey_limit`, `apikey_usage` + `apikey_usage_period` (written here, period string such as `2026-08`), `apikey_limit_reset_period` (default `monthly`), `apikey_limit_start_date`, `status` (values in `src/configs/constant.py:api_key_status`) |
| `folders` | `_id`, `org_id`, `type`, `config` (`tools_id`, `pre_tool_id`, `post_tool`, `prompt`, `response_type`), `apikey_object_id`, `folder_limit` / `folder_usage` / `folder_limit_reset_period` / `folder_limit_start_date`, `variables_path`, `wrapper_id` |
| `preBuiltPrompts` | org docs: `org_id`, `prebuilt_prompts.{key}`; global docs: top-level keys (`worker_prompt`, `synthesizer_prompt`, `planner_prompt`), newest `_id` wins |
| `agent_memories` | `resource_id`, `original_answer` (gtwy-node also stores agent_id, canonical_question, frequency, last_seen) |
| `testcases` | `_id`, `bridge_id`, `conversation`, `type`, `expected`, `matching_type`, `variables`, `execution.lastExecutedAt`, `updatedAt` |
| `modelconfigurations` | `service`, `model_name`, `status`, `org_id` (optional), `configuration`, `validationConfig`, `outputConfig` (pricing per 1M tokens). See [llm-providers](modules/llm-providers.md#the-modelconfigurations-collection) |
| `services` | `service_name`, `wire_format`, `client`, `base_url`, capability flags, `status`. See [llm-providers](modules/llm-providers.md#the-services-collection-drives-behaviour) |
| `billing_plans` | `plan_code`, `status`, `services`, `credit_grant`, `hit_fees`. See [billing](modules/billing-and-limits.md#billing-plans) |
| `gtwy_browser_cookies` | `scope_key`, `cookies` (encrypted hex), `cookie_count`, `version` (1), `updated_at`, `expires_at` (now + `COOKIE_TTL_SECONDS`, 30 days), optional `org_id`, `bridge_id`. See [gtwy-browser](modules/gtwy-browser.md#remembered-logins) |

How the agent pipeline joins these collections is in [agent-configuration](modules/agent-configuration.md#loading-the-agent-get_bridges_with_tools_and_apikeys).

> [!WARNING]
> **`preBuiltPrompts` may not be the collection gtwy-node writes.** Python reads `db["preBuiltPrompts"]`. gtwy-node's model is named "preBuiltPrompts" with no explicit collection, and Mongoose lowercases model names, so its prompt editor reads and writes `prebuiltprompts`. Mongo names are case-sensitive. Org overrides saved from the UI (for example `structured_output_optimizer`, read by `get_specific_prebuilt_prompt_service`) may never reach Python.

> [!TODO] Needs a human: in production, which of `preBuiltPrompts` / `prebuiltprompts` holds the org overrides and the global todo-mode prompts?

> [!WARNING]
> **Agent docs are cached in Redis for 2 days.** Anything read through `get_bridges_with_tools_and_apikeys`, including `apikey_usage`, is as old as the cache entry. That is why `get_usage` reads `apikeycredentials` straight from Mongo. A write to a joined collection reaches Python only after gtwy-node purges the tag (see [Cache](#tag-invalidation)).

> [!WARNING]
> **Registries load once and then follow change streams.** `services`, `modelconfigurations`, `platform_apikeys` and `billing_plans` are read at startup (`index.py:lifespan`). Each `watch()` listener reloads the whole collection on any change, and reconnects after 5 to 10 s when the stream drops (`background_listen_for_changes` and its siblings). A change made while a stream is down is not replayed: it shows up only with the next change or a restart. Change streams need a replica set.

> [!WARNING]
> **The direct agent relies on gtwy-node's unique (org_id, slugName) index.** `get_or_create_direct_agent` upserts with no `deletedAt` filter, so a soft-deleted direct agent is restored rather than duplicated. A second insert would collide with that index until the 30-day TTL removes the old doc.

## Postgres

Connection: `models/postgres/pg_connection.py` builds a **synchronous** SQLAlchemy engine (`postgresql+psycopg2`) and `Session`; `models/index.py:combined_models` exposes it as `pg`. Models: `models/postgres/pg_models.py`. Python only **reads** `conversation_logs`. History rows are written by gtwy-node from the log queue (message keys in [queues-history-metrics](modules/queues-history-metrics.md#contracts)).

| Table | Purpose | Python access | gtwy-node model · migration |
|---|---|---|---|
| `conversation_logs` (`ConversationLog`) | One row per turn: user message, LLM reply, tools, tokens, status. Also batch rows (`batch_data`) and plan-mode plans (`plans`). | read: `src/db_services/conversationDbService.py:find_conversation_logs` (last 3 with `status` true, via `getThread` on a conversation-cache miss), `find_completed_batch_conversations` (skips `batch_data->>'status' = 'queued'`), `find_rerun_logs`. `update_conversation_log` has no callers. | models/postgres/conversation_logs.js; created by migrations/pg/20251115114058-new_table_for_orchreattrator_andconversation.cjs, plus later column migrations |
| `orchestrator_conversation_logs` (`OrchestratorConversationLog`) | One row per multi-agent (orchestrator) turn; most columns are JSON keyed by bridge id. | none (published as `save_orchestrator_history`) | models/postgres/orchestrator_conversation_log.js; migrations/pg/20260622120000-create-orchestrator-conversation-logs.cjs |
| `system_prompt_versionings` | Prompt history per agent. | none (`storeSystemPrompt` has no callers) | models/postgres/system_prompt_versionings.js; migrations/pg/20240504071205-System_prompt_versioning.js |
| `user_bridge_config_history` | Who changed an agent, and when. | none (`add_bulk_user_entries` has no callers) | models/postgres/user_bridge_config_history.js; migrations/pg/20241104103712-create_table_user_config_history.cjs |

Columns as declared in `models/postgres/pg_models.py` (gtwy-node's model has more, such as is_cached, display_name, testcase_id, testcase_data):

| Table | Columns (type) |
|---|---|
| `conversation_logs` | `id` Integer PK · `llm_message`, `reasoning`, `user`, `chatbot_message`, `updated_llm_message`, `error`, `firstAttemptError` Text · `user_feedback` Integer (0) · `tools_call_data`, `annotations`, `user_urls`, `llm_urls` JSON lists · `AiConfig`, `fallback_model`, `tokens`, `variables`, `latency`, `prompt`, `batch_data`, `plans`, `response_format` JSON · `message_id`, `thread_id`, `sub_thread_id`, `version_id`, `bridge_id`, `org_id`, `service`, `model`, `finish_reason`, `parent_id`, `child_id` String · `status` Boolean (false) · `created_at`, `updated_at` DateTime |
| `orchestrator_conversation_logs` | `id` PK · per-agent JSON maps `llm_message`, `reasoning`, `user`, `chatbot_message`, `updated_llm_message`, `prompt`, `error`, `tools_call_data`, `message_id`, `version_id`, `bridge_id`, `AiConfig`, `fallback_model`, `model`, `status`, `tokens`, `variables`, `latency`, `firstAttemptError`, `finish_reason` · `image_urls`, `urls` JSON lists · `thread_id`, `sub_thread_id`, `org_id`, `service` String · `agents_path` ARRAY(String) · timestamps |
| `system_prompt_versionings` | `id` PK · `system_prompt` Text · `bridge_id`, `org_id` String · timestamps |
| `user_bridge_config_history` | `id` PK · `user_id` Integer · `org_id`, `bridge_id`, `type` String · `time` DateTime · `version_id` String ("") |

Indexes (gtwy-node migrations): `conversation_logs` has (org_id, bridge_id, thread_id, sub_thread_id), (org_id, bridge_id, updated_at DESC), message_id, testcase_id, and trigram/GIN search indexes on user, llm_message, variables and batch_data->>'batch_id'. `orchestrator_conversation_logs` has (org_id, bridge_id), thread_id, created_at. `user_bridge_config_history` has (org_id, type, bridge_id, time DESC).

> [!WARNING]
> **Python never creates or migrates tables.** `init_dbservice` calls `Base.metadata.create_all` at import, before `pg_models.py` defines any class, so it creates nothing. A new column needs a gtwy-node migration plus its Node model; add it to `pg_models.py` only if Python reads it. A new key in the history payload is dropped by gtwy-node until its consumer maps it.

> [!WARNING]
> **Postgres must be reachable at import time.** `pg_connection.py` runs `metadata.reflect(bind=engine)` at module level with no `try`, so importing `models.index` fails when Postgres is down. The session is synchronous: every query blocks the event loop for its duration.

## Timescale

`models/Timescale/connections.py` builds an async engine from `TIMESCALE_SERVICE_URL`; `index.py:lifespan` only runs `init_async_dbservice` (`SELECT 1`). `models/Timescale/timescale_models.py:Metrics_model` maps `metrics_raw_data` but nothing imports it: Python never reads or writes Timescale. The rows come from the metrics queue (`save_metrics`), and gtwy-node bulk-inserts them.

| Table | Columns (type) | Owner |
|---|---|---|
| `metrics_raw_data` | `id` Integer PK · `org_id`, `bridge_id`, `version_id`, `thread_id`, `model`, `service`, `apikey_id`, `time_zone` String · `input_tokens`, `output_tokens`, `total_tokens`, `latency`, `cost` Float · `success` Boolean · `created_at` DateTime | gtwy-node models/timescale/rawdata.js; migrations/timescale/20240223052349-raw_data.cjs (hypertable on created_at, unique (created_at, id)); 20240301071935-add-retention-policy.cjs keeps raw rows 1 day, rolled up into fifteen_minute_data and daily_data |

## Redis

All helpers in `src/services/cache_service.py` prepend `REDIS_PREFIX` = `AIMIDDLEWARE_{ENVIRONMENT}_`. Code that uses the raw `client` adds it by hand: `src/configs/blocked_orgs.py`, `src/services/billing/billing_utils.py`, `src/services/cache_utils.py`. gtwy-node uses the same prefix (its src/cache_service/index.js), which is what makes the shared keys below work. Values are JSON strings unless noted; `find_in_cache` returns the raw string. Default TTL is `DEFAULT_REDIS_TTL` (172800 s, 2 days). Helper behaviour: [queues-history-metrics](modules/queues-history-metrics.md#5-redis-helpers-srcservicescache_servicepy).

### Naming: `cd_` and `nd_`

Key prefixes live in `src/configs/constant.py:redis_keys`; gtwy-node mirrors them in its src/configs/constant.js. Shared names must stay byte-identical in both repos.

- `cd_` (can delete): a cache of DB data, rebuilt on a miss.
- `nd_` (no delete): Redis is the only copy, or it is a counter (spend, rate limit, plans, billing, browser state).
- gtwy-node's bulk "clear Redis" (src/controllers/utils.controller.js) deletes every prefixed key **except** `nd_…` and `blacklist:…`. Keys with neither (tags, `template_`, locks, GPT memory, `mcp_tools_list:`) are wiped. A new key that must survive that needs the `nd_` prefix.

The prefixes went in with commit 6008933f ("redis prefix added to deelte and not delete").

### Key catalogue

Keys are shown after `AIMIDDLEWARE_{ENVIRONMENT}_`. "Node" says what gtwy-node does with the same key.

**Agent config and lookups**

| Key | Value · TTL | Written by | Read / invalidated by | Node |
|---|---|---|---|---|
| `cd_bridge_data_with_tools_{org_id}_{bridge or version}_{id}[_env_{environment}]` | `{"success", "bridges": {...}}`, keys still encrypted · 2 d | `get_bridges_with_tools_and_apikeys` via `store_in_cache_with_tags` | same function; purged by tag | deletes (tag purge, and directly on version or limit edits) |
| `tag:{agent, version, tool, apikey, folder, connected_agent, wrapper, rag}:{id}` | set of full blob keys · blob TTL + 600 s | `src/services/cache_utils.py:store_in_cache_with_tags` | gtwy-node only | reads and deletes |
| `cd_direct_agent_{org_id}` | agent id · 30 d | `get_or_create_direct_agent` | same | none |
| `embed:org_{org_id}` | MSG91 company record, including `meta.gtwyAccessToken` (the org's embed signing secret) · 2 d. No `cd_`/`nd_` prefix, so a bulk clear removes it (it is rebuilt on the next call) | `src/services/proxy/embed_service.py:resolve_gtwy_embed_token` | same; never invalidated here | none known |
| `embed:user_{embed_user_id}:{org_id}:{folder_id}` | `{proxyResponse, name, email}` of the MSG91 user created for an embed token · 2 d | `src/services/proxy/embed_service.py` | same | none known |
| `cd_timezone_and_org_{org_id}` | MSG91 company dict, `{}` on failure · 2 d | `src/services/proxy/Proxyservice.py:get_timezone_and_org_name` | same; never invalidated | none |
| `userOrgMapping-{user_id}-{org_id}` | MSG91 user dict · 1 h | `get_user_org_mapping` (for `send_alert`) | same | same key, same use |
| `template_{template_id}` | `templates` doc · 2 d | `get_template_by_id` | same; never invalidated | none |
| `mcp_tools_list:{sha256(url + headers)}` | `{"tools": [...]}` · 600 s | `src/services/mcp_gateway/prefetch.py:_fetch_server_tools` | same | none |
| `blacklist:{token}` | revoked JWT marker · remaining token life | gtwy-node when it revokes a token | `src/services/token_service.py:is_token_blacklisted` | writes |
| `cd_get_bridge_data_{id}` | dead: `get_bridges_with_redis` has no callers | none | none | deletes `cd_get_bridge_data_{org_id}_{id}` (other shape) |

**Conversation state**

| Key | Value · TTL | Written by | Read by | Node |
|---|---|---|---|---|
| `cd_conversation_{version_id or bridge_id}_{thread_id}_{sub_thread_id}` | last ≤ 9 `{role, content, user_urls?}` · 2 h | `src/db_services/metrics_service.py:save_conversations_to_redis` | `manage_threads`; a miss falls back to Postgres | none |
| `cd_last_transffered_agent_{primary_bridge_id}_{thread_id}_{sub_thread_id}` | agent id · 3 d | `src/services/commonServices/common.py:chat` | `chat_multiple_agents` | none |
| `{thread_id}_{sub_thread_id}_{version_id or bridge_id}` (GPT memory, no class prefix) | memory payload · 2 d | `src/services/utils/gpt_memory.py:get_gpt_memory` after a remote fetch | `get_gpt_memory` | writes the same key after a memory update |
| `nd_gpt_memory_counter_{same id}` | integer (INCR) · 2 d from the first turn | `make_request_data_and_publish_sub_queue` | same; memory update fires every `GPT_MEMORY_TURNS_PER_CYCLE` turns | none |
| `nd_metrix_bridges_{bridge_id}` | float, running token total · 2 d, reset on every write | `_update_history_redis` | nobody, in either repo | none |
| `nd_plan_{org}_{bridge}_{thread}_{sub}`, `nd_plan_session_{…same}` | plan JSON; planner Q&A · `PLAN_TTL` 48 h, renewed on save | `src/services/todo/plan_store.py:save_plan`, `_persist_session` | `get_plan`, `get_planner_session`; `delete_plan`, `clear_planner_session` | none. See [todo-mode](modules/todo-mode.md#data-the-plan-in-redis) |
| `nd_batch_{batch_id}` | batch job: provider key, webhook, message map, ids · 24 h | provider batch handlers, e.g. `src/services/commonServices/openAI/openai_batch.py` | `src/services/utils/batch_script.py:check_batch_status` (KEYS scan every 900 s); deleted when done | none |
| `nd_rate_limit_{value}` | `{"count": n}` · 60 s window, not extended by hits | `src/middlewares/ratelimitMiddleware.py:rate_limit` | same | own keys, `nd_rate_limit_{keyPrefix}:{ip}` |

**Spend limits and usage alerts** ([billing](modules/billing-and-limits.md#spend-limits-agent-folder-api-key))

| Key | Value · TTL | Written by | Read by | Node |
|---|---|---|---|---|
| `nd_bridgeusedcost_{bridge_id}` | `{usage_value, versions, reset_period, setup_date}` · until the next reset (`calculate_limit_ttl`) | `src/services/utils/update_and_check_cost.py:update_usage_cost_in_cache`; seeded by `_check_limit` | `_check_limit`, `_current_usage` | deletes on limit edit; never copied to Mongo |
| `nd_folderusedcost_{folder_id}` | same plus `bridges` | same | same | reads, deletes |
| `nd_apikeyperiodcost_{apikey_id}_{period}` | bare float (INCRBYFLOAT) · `redis_ttl_for`, set once | `_write_apikey_usage`; reseeded from Mongo by `_apikey_usage_for_period` | `_apikey_usage_for_period`, `_current_usage` | reads; deletes every period on limit change |
| `nd_apikeyperiodrefs_{apikey_id}_{period}` | set of `v:{version_id}`, `b:{bridge_id}` · same | `_write_apikey_usage` | gtwy-node, to purge agent caches | reads |
| `nd_bridgelastused_{bridge_id}`, `nd_apikeylastused_{apikey_id}` | datetime · 2 d | `update_last_used` | gtwy-node cron (every 15 min) copies to Mongo | reads |
| `nd_dailyusedcost_{scope}_{id}_{YYYYMMDD}` | float · 8 d | `src/services/utils/usage_alert_service.py:_bump_daily_bucket` | `_read_bucket` (spike check) | none |
| `nd_apikeyusedcost_` | unused since API-key spend moved to period keys | none | none | declared only |

**Billing** ([billing](modules/billing-and-limits.md#credit-hold-lifecycle); the braces are literal Redis Cluster hash tags)

| Key | Value · TTL | Written by | Read by | Node |
|---|---|---|---|---|
| `nd_billing_credit_balance_{<org_id>}` | shadow balance float · no TTL | `_sync_balance_from_lago` (SET NX), Lua scripts | `reserve_credits`, `apply_debit`, `release_credits` | writes (SET and INCRBYFLOAT) |
| `nd_billing_credit_hold_{<org_id>}_{token}` | held amount · 7200 s | `reserve_credits` | `release_credits` | none |
| `nd_billing_credit_applied_{<org_id>}_{transaction_id}` | debit claim · 86400 s | `apply_debit` | same | writes the same claims |
| `nd_billing_no_wallet_{org_id}` | "1" · 60 s | `_sync_balance_from_lago` | same | none |
| `nd_org_billing_plan_{org_id}` | plan slug · 1 d (60 s after a failure) | `get_org_plan` | same | reads; deletes on plan change |
| `nd_blocked_orgs_` | set of org ids · no TTL | gtwy-node blocked-org endpoints (SADD / SREM; it also keeps a blocked_orgs Mongo collection) | `src/configs/blocked_orgs.py:is_org_blocked` (SISMEMBER) | writes |

**Gtwy_Browser** ([gtwy-browser](modules/gtwy-browser.md#contracts)): `nd_gtwy_browser_registry_{host-slug}` (5400 s) and `nd_gtwy_browser_thread_{org}:{thread}:{sub}` (3600 s), both in `src/services/utils/built_in_tools/browser/session_store.py`. This repo only.

**Locks.** `acquire_lock` stores `lock_{name}` with SET NX EX and `release_lock` deletes it without an owner check:

| Lock key | TTL | Holder |
|---|---|---|
| `lock_{batch_id}` | 1800 s | `check_batch_status` |
| `lock_gtwy_browser_registry_{host-slug}` | 30 s | `acquire_registry_lock` |
| `lock_` + `REAPER_LOCK` ("gtwy_browser_reaper") | 55 s | `src/services/utils/built_in_tools/browser/reaper.py:reap_idle_tabs` |
| `lock_nd_usagealertsent_{scope}_{id}_{threshold or limit}` | until the limit resets | `_claim_alert` (one email per period) |
| `lock_nd_usagespikealert_{scope}_{id}_{YYYYMMDD}` | 2 d | `_check_spike` |

### Tag invalidation

Python only writes tags. When gtwy-node saves an agent, version, tool, API key, folder, wrapper or RAG collection, its Mongoose cache-invalidation plugin calls invalidateByTag, which deletes every blob in the tag set and then the set. A collection newly joined into the agent pipeline needs a tag in `src/services/cache_utils.py:extract_cache_tags` and in gtwy-node's src/configs/tagKeys.js, or its edits stay invisible for up to 2 days. Details: [queues-history-metrics](modules/queues-history-metrics.md#6-tag-based-invalidation-srcservicescache_utilspy).

> [!WARNING]
> **`redis_keys['pdf_url_']` raises `KeyError`.** `src/controllers/image_process_controller.py:_cache_pdf_urls` still uses the key that commit 6008933f removed from `redis_keys`. Uploading a PDF with `thread_id` and `agent_id` fails after the file is already in GCP.

> [!WARNING]
> **Thread files are never found.** `add_files_to_parse_data` reads `{bridge_id}_{thread_id}_{sub_thread_id}`, but gtwy-node's save_files_to_redis job writes `nd_files_{bridge_id}_{thread_id}_{sub_thread_id}`. The lookup always misses.

> [!WARNING]
> **`nd_batch_` keys hold the provider API key in plain text** for up to 24 hours, so anyone with Redis read access can read it. If a batch outlives that TTL, the cron forgets it and its results are never collected.

> [!WARNING]
> **Rate-limit keys are not scoped.** `rate_limit` keys on the raw value (a `bridge_id`, `thread_id`, `slugName` or org id), so the same thread id sent to two agents or two orgs shares one counter.

> [!WARNING]
> **Bulk clear wipes the usage-alert locks.** They start with `lock_`, not `nd_`. After a clear, the next request over a threshold sends the email again.

## RabbitMQ

One robust connection (`QUEUE_CONNECTIONURL`) with a channel per queue, in `src/services/commonServices/queueService/baseQueue.py`. Publishing is JSON on the default exchange, persistent, with a `retry_count` header, tried 3 times. Mechanics and message contracts: [queues-history-metrics](modules/queues-history-metrics.md#1-the-three-queues).

| Queue | Name: env var, else fallback | Producer | Consumer | Message | On failure |
|---|---|---|---|---|---|
| Main (`queue_obj`) | `QUEUE_NAME`, else `AI-MIDDLEARE-DEFAULT-{ENVIRONMENT}` | `src/routes/v2/modelRouter.py` (non-default `response_format`), `queue_rerun_messages` | this repo, `Queue.process_messages`, only when `CONSUMER_STATUS` is true; prefetch `PREFETCH_COUNT` | `{"body": {..., "message_id"}, "state": {"profile", "version", "timer"}, "path_params", "id_to_use"}` from `make_request_data`; `body.configuration.type == "image"` goes to `image` | message acked; `{"error", "original"}` published to `<QUEUE_NAME>-Failed`, which nothing consumes |
| Log (`sub_queue_obj`) | `LOG_QUEUE_NAME`, else `AI-MIDDLEARE-DATA-QUEUE-{ENVIRONMENT}` | `process_background_tasks`, error paths, reviewer rounds, batch, todo mode | gtwy-node src/consumers/logQueueConsumer.js (prefetch 10) | one dict with keys such as `save_history`, `save_orchestrator_history`, `billing`, `update_history`, `save_batch_history` | gtwy-node nacks without requeue, so the message is dropped. Python declares `<name>-Failed` but nothing uses it |
| Metrics (`metrics_queue_obj`) | `METRICS_QUEUE_NAME`, else `AI-MIDDLEWARE-METRICS-QUEUE-{ENVIRONMENT}` | same publishers | gtwy-node src/consumers/metricsQueueConsumer.js (batches of 100) into `metrics_raw_data` | `{"save_metrics": [row, ...]}` with the `metrics_raw_data` columns | a failed bulk insert goes to `<METRICS_QUEUE_NAME>-failed` (lowercase, gtwy-node) |

> [!WARNING]
> The `MIDDLEARE` typo in two fallback names is load-bearing: renaming orphans whatever is queued. gtwy-node reads `LOG_QUEUE_NAME` and `METRICS_QUEUE_NAME` with no fallback, so set both explicitly in both repos. Python never declares the metrics queue; a publish before gtwy-node has asserted it is silently lost.

## GCP Cloud Storage

`src/services/utils/gcp_upload_service.py:uploadDoc` builds a client from the `GCP_CREDENTIALS` JSON on every call and writes to bucket `resources.gtwy.ai`. The returned URL is `https://resources.gtwy.ai/{folder}/{uuid}.{ext}`. Nothing in this repo reads or deletes objects.

| Folder | Written by |
|---|---|
| `folder="uploads"` | `src/controllers/image_process_controller.py` (`_upload_single_image`, file uploads that are not videos) |
| `folder="generated-images"` | `src/services/commonServices/openAI/image_model.py`, `src/services/commonServices/openAI/openai_response.py`, `src/services/commonServices/Google/gemini_image_utils.py` |

> [!WARNING]
> With `real_time=False` (OpenAI image URLs in `image_model.py`), `uploadDoc` returns the URL **before** the upload runs in a background task, and a failure is only printed. The URL can 404 for a moment, or for good. The google-cloud-storage calls are synchronous and block the event loop while they run.

## Changing it safely

1. **New Redis key**: add it to `redis_keys` with `cd_` (rebuildable) or `nd_` (must survive a bulk clear), always pass a TTL, and add it to this catalogue. If gtwy-node reads or deletes it, add the same name to its src/configs/constant.js.
2. **New field on a Mongo doc**: the schema change belongs in gtwy-node. If Python reads the field through the agent pipeline, it is cached for 2 days unless the owning collection is tag-invalidated.
3. **New history or metrics field**: add it to the payload builders in `src/db_services/metrics_service.py`, then to gtwy-node's consumer column list and Postgres migration. Until that ships, the field is silently dropped.
