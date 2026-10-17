---
covers: ["src/middlewares/**", "src/services/utils/getConfiguration.py", "src/services/utils/getConfiguration_utils.py", "src/services/utils/getDefaultValue.py", "src/db_services/ConfigurationServices.py", "src/services/direct_agent_service.py", "src/services/proxy/**", "src/services/token_service.py", "src/services/commonServices/generateToken.py", "src/services/prebuilt_prompt_service.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Agent configuration

This area authenticates a request, rate-limits it, and turns "this request + this agent id" into the full per-agent configuration the chat pipeline runs on: the Mongo agent doc (or one of its versions), decrypted API keys, tools, connected agents and request overrides. FastAPI dependencies in `src/routes/v2/modelRouter.py` and `src/routes/chatBot_routes.py` call it before any handler runs. It also owns the hidden per-org agent behind `POST /api/v2/model/ai/completion`.

"Bridge" and "agent" mean the same thing: one doc in the `configurations` collection. A "version" is a doc in `configuration_versions` whose `parent_id` is the agent id. Publishing in gtwy-node copies a version over the agent doc and sets `published_version_id`. So a call with only a bridge id runs the published config, and a call with a `version_id` runs that version.

| File | Role |
|---|---|
| `src/middlewares/middleware.py` | `jwt_middleware`: JWT, gtwy embed token, `proxy_auth_token` or `pauthkey` auth. Checks the token blacklist and blocked orgs. Sets `request.state.profile`. |
| `src/middlewares/ratelimitMiddleware.py` | `rate_limit`: fixed-window counter in Redis, keyed by a value read from the body, profile or headers. |
| `src/middlewares/getDataUsingBridgeId.py` | `add_configuration_data_to_body`: validates the body, calls `getConfiguration`, reserves credits, merges the primary agent config into the request body. |
| `src/middlewares/interfaceMiddlewares.py` | Chatbot interface: `chat_bot_auth` (chatbot JWT) and `send_data_middleware` (resolves the agent by slug, then calls `chat_completion`). |
| `src/middlewares/agentsMiddlewares.py` | `agents_auth`: the public-agent token fallback for the chatbot route. |
| `src/middlewares/openai_sdk_middleware.py` | `POST /api/v2/model/openai/responses`: rewrites an OpenAI Responses body into the internal shape, maps `Bearer <pauthkey>` to a `pauthkey` header, then auth + rate limit. |
| `src/services/utils/getConfiguration.py` | `getConfiguration`: builds `bridge_configurations` for the primary agent plus every connected and reviewer agent. |
| `src/services/utils/getConfiguration_utils.py` | Helpers: `validate_bridge`, `setup_configuration`, `setup_api_key`, `setup_tools`, `add_rag_tool`, `add_web_crawling_tool`, `add_browser_tool`, `add_connected_agents`, `strip_gateway_filled_args`. |
| `src/db_services/ConfigurationServices.py` | `get_bridges_with_tools_and_apikeys` (the big aggregation plus the folder merge, cached in Redis), plus slug, template and prompt-wrapper lookups. |
| `src/services/direct_agent_service.py` | `get_or_create_direct_agent`, `fill_model_defaults`: the hidden per-org agent behind direct calls. |
| `src/services/proxy/Proxyservice.py` | MSG91 calls: token details, `pauthkey` validation, org timezone and name, user lookup for alerts. |
| `src/services/proxy/embed_service.py` | `resolve_gtwy_embed_token`: verifies a gtwy embed JWT with the org's own secret and finds or creates the embed user in MSG91. A port of gtwy-node's resolveGtwyEmbedToken. |
| `src/services/token_service.py` | `is_token_blacklisted`: reads the `blacklist:{token}` key that gtwy-node writes on logout. |
| `src/services/prebuilt_prompt_service.py` | Reads `preBuiltPrompts`. Used by the structured-output formatter and todo mode. |
| `src/services/utils/getDefaultValue.py`, `src/services/commonServices/generateToken.py` | Not imported anywhere in this repo (dead code). |

## How it works

### Auth paths

| Route | Dependency | How the caller proves identity |
|---|---|---|
| `POST /api/v2/model/chat/completion`, `/batch/chat/completion`, `/testcases`, `/rerun` | `src/routes/v2/modelRouter.py:auth_and_rate_limit` → `jwt_middleware` | `Authorization: <jwt>` signed with `SecretKey` (HS256). Or a gtwy embed token in `Authorization`: a JWT with a top-level `folder_id`, signed with the org's own secret (see below). Or a `proxy_auth_token` header (MSG91 `getDetails`). Or a `pauthkey` header (MSG91 `validateCauthKey`). |
| `POST /api/v2/model/ai/completion` | `src/routes/v2/modelRouter.py:direct_auth_and_rate_limit` | Same as above. |
| `POST /api/v2/model/openai/responses` | `openai_sdk_middleware` | `Authorization: Bearer <pauthkey>`. `convert_bearer_to_local_auth` rewrites it to a `pauthkey` header before `jwt_middleware` runs. |
| `POST /chatbot/{botId}/sendMessage` | `src/routes/chatBot_routes.py:combined_auth` | First `chat_bot_auth`: the JWT is checked with `CHATBOTSECRETKEY`, or with `PUBLIC_CHATBOT_TOKEN` when `botId == "Public_Agents"`. If that raises an `HTTPException`, `agents_auth` tries `PUBLIC_CHATBOT_TOKEN`. |
| `POST /rag/query`, `POST /files/upload` | `jwt_middleware` only | Same as the model routes. No rate limit. |

`jwt_middleware` stringifies `org.id` and sets `request.state.profile`, `org_id`, `user_id`, `folder_id` (from `extraDetails.folder_id`) and `embed`. It rejects blacklisted tokens (401) and orgs in the blocked set (403, `is_org_blocked`, owned by [billing-and-limits](billing-and-limits.md)). JWT claims are copied into the profile as-is. The proxy paths build the profile themselves:

```python
{"ip": "9.255.0.55",                       # hard-coded
 "user": {"id": str, "name": str, "is_embedUser": bool, "folder_id": str|None, "email": str|None},
 "org":  {"id": str, "name": str},
 "authkey": {...},                          # pauthkey path only
 "extraDetails": {"proxy_auth_type": "pauthkey"}}   # pauthkey path only
```

**Embed tokens.** `jwt_middleware` first decodes the `Authorization` JWT without checking the signature. A normal gtwy token nests `folder_id` under `extraDetails`, so a top-level `folder_id` marks an embed token, and `resolve_gtwy_embed_token` handles it:

1. The unverified claims must hold `org_id`, `folder_id` and `user_id` (or `unique_identifier`). Otherwise 401.
2. `get_organization_by_id` reads the org's MSG91 company record (`getCompanies`, cached in `embed:org_{org_id}`). Its `meta.gtwyAccessToken` is the org's own HS256 secret. No secret → 401 "invalid token". A bad signature → 404 "token verification failed", as in gtwy-node.
3. The external user id is passed through `encrypt_string` (uppercase plus letter codes for special characters, not real encryption), so the raw id stays out of the synthetic email `{org_id}_{folder_id}_{user}@gtwy.ai`.
4. `create_or_get_user` calls MSG91 `createCUsers` with `PROXY_USER_ROLE_ID` and caches the reply in `embed:user_{user_id}:{org_id}:{folder_id}`. The key uses the raw id from the token.

The profile it returns:

```python
{"user": {"id": "<MSG91 user id>", "name": str, "meta": {"type": "embed", ...}},   # meta.type sets request.state.embed
 "org":  {"id": "<company id from the createCUsers reply>", "name": "<org name from getCompanies>"},
 "extraDetails": {"type": "embed", "folder_id": str}}                              # becomes request.state.folder_id
```

`org.id` comes from the `createCUsers` reply, not from the token's `org_id` claim. Both caches use the default 2-day TTL, and the key strings match gtwy-node's embed_cache keys.

### Rate limits

`rate_limit(request, key_path, points, ttl=60)` reads the value at `key_path` and counts hits in `nd_rate_limit_{value}`. Past `points` it returns 429 with `Retry-After`. A missing value skips the check.

| Route | Keys (hits per 60 s) |
|---|---|
| model routes (`auth_and_rate_limit`) and `openai/responses` | `body.bridge_id` 100, `body.thread_id` 20 |
| `ai/completion` | `profile.org.id` 100, `body.thread_id` 20 |
| chatbot, first path | `body.slugName` 100, `body.threadId` 20 |
| chatbot, public fallback | `state.profile.limiter_key` 10 (never applied, see Gotchas) |

> [!TODO] Needs a human: why 100 per agent, 20 per thread and 10 per public user? No commit records the reason.

### Building the configuration (`add_configuration_data_to_body`)

1. Validate the body against the schema class passed in (`CompletionRequest` or `BatchChatCompletionRequest`; `None` skips it). A failure is a 422.
2. Merge `request.state.chatbot` (set by `send_data_middleware`) into the body.
3. Agent id = `agent_id` → `bridge_id` → path param → chatbot `bridge_id`. Version = `version_id` from the body or the path.
4. Call `getConfiguration(...)`. If it returns `success: False` or `error`, answer 400 with that dict as `detail`.
5. `reserve_credits_and_api_key_setup` ([billing-and-limits](billing-and-limits.md)) fills in a platform key for agents without their own key, sets `wallet` on each config and `billing_attribution` on `db_config`, and may place a credit hold.
6. Pick the primary config: `bridge_configurations[bridge_id or primary_bridge_id]`, else the first entry.
7. If the profile has no `owner_id`, derive it: `org_id`, or `{org_id}_{folder_id}_{user_id}` when the agent has both. RAG queries use it.
8. Direct calls only (`request.state.is_direct_call`): request `settings` are merged over the agent's, and `settings.response_format` defaults to `{"type": "default"}`.
9. `body.update(primary_config)`. Then the request's `wrapper_id` and `settings.response_format` are put back, an explicit `stream` is copied into `configuration.stream`, and `bridge_configurations`, `billing_attribution` and `credit_hold_token` are added.
10. Final checks: there must be a user message (or images, audio from `user_urls`, a batch, or a rerun), the service/model must exist in `model_config_document`, and an org-private model must belong to this org. Any failure releases the credit hold.

The handler then reads the merged body with `make_request_data`, which calls `request.json()` again and gets the same, now mutated, dict. See [chat-orchestration](chat-orchestration.md) and [the chat completion flow](../flows/chat-completion.md).

### Inside `getConfiguration`

`_prepare_configuration_response` builds one agent's config:

1. Load the agent with `get_bridges_with_tools_and_apikeys` (next section). Then `validate_bridge`: no doc → "Agent does not exist in this organization", `deletedAt` set → "Agent has been deleted", `bridge_status == 0` → raises "Agent is Currently Paused".
2. Check spend limits with `check_bridge_api_folder_limits` ([billing-and-limits](billing-and-limits.md)).
3. `setup_configuration`: request `configuration` keys are shallow-merged over the DB configuration. A dict prompt (role/goal/instruction) becomes a string. In a folder agent, a folder `customPrompt` replaces it. `service` comes from the request, else from the agent. `openai_response` becomes `openai`. An agent with `openai_completion` set uses service `openai_completion`.
4. `transform_agent_config_to_frontend`: `{"mode": "custom", "value": x}` becomes `x`, and any other `{"mode": m}` becomes `m` (`"default"`, `"min"`, `"max"`).
5. Keys: `apikeys_combined` and `folder_apikeys` are normalized. `setup_api_key` picks the key (order below). `service_apikeys` holds every decrypted key by service.
6. An `image` type config returns early with a small config (no tools).
7. Tools, in this order: one per `connected_tools` entry of type `tools` → folder tools (`apiCalls` entries that have no `connected_tools` entry) → request `extra_tools` → `get_knowledge_base_data` when `connected_tools` has `docs` entries → `Gtwy_Web_Search` → `Gtwy_Browser` (only if a Steel host is set up, see [gtwy-browser](gtwy-browser.md)) → one tool per `connected_tools` entry of type `agent`. The built-in list is the request's `built_in_tools`, else the `built_in_tools` entry in `connected_tools`. `tool_id_and_name_mapping` tells [tool-calling](tool-calling.md) how to run each tool by name. `apiCalls` tools post to the `url` stored on their `apicalls` doc.
   Pre-tools and the post-tool are resolved here too (see [`connected_tools`](#connected_tools)).
8. Tone and response-style prompts, the RAG doc description and `current_time_date_and_current_identifier` are added. The time comes from the org timezone that `updateVariablesWithTimeZone` fetches from MSG91.

`getConfiguration` then calls `_collect_connected_agent_configs`, which builds every child agent (in parallel, recursively, with a `visited` set to stop loops) and the `settings.review_agent.reviewer_agent`. Children come from `connected_tools` entries of type `agent`, plus the legacy `connected_agents` map, which is still read for versions saved before the switch. Each child gets its own entry in `bridge_configurations`. A `connected_tools` entry passes down only the child id and `environment`, merged over the child's `agent_info`. A legacy `connected_agents` entry can also override the child's `configuration`, `service`, `apikey`, `variables`, and so on. A child that fails is logged and skipped.

**API key order** (`setup_api_key`): request `apikey` → folder key for the service → agent key for the service (`openai_completion` uses the `openai` key) → for a chatbot agent on `gpt-5-nano` with no key, `OPENAI_API_KEY_GPT_5_NANO` → `None`, which billing then fills with a platform key.

### `connected_tools`

An agent or version doc lists everything attached to it in one `connected_tools` array, written by gtwy-node (#1752, #1781). The pipeline and `_prepare_configuration_response` read it by `type`:

```python
"connected_tools": [
  {"type": "tools", "id": "<apicalls _id>", "variable_path": {...}},
  {"type": "agent", "id": "<child agent id>", "environment": str, "thread_id": bool,
   "description": str, "variables": {"fields": {...}, "required": [...]}, "variable_path": {...}},
  {"type": "pre_tool", "pre_tool_type": "custom_function" | "<other>", "id": "<apicalls _id>",
   "variable_path": {...}, "args": {...}, "url"?, "title"?, "script_id"?},
  {"type": "post_tool", "id": "<apicalls _id>", "variable_path": {...}, "args": {...}, "url"?, "title"?, "script_id"?},
  {"type": "docs", "id": "<resource id>", ...},        # copied into rag_data; resource_id defaults to id
  {"type": "built_in_tools", "built_in_tools": [...] | str, "gtwy_web_search_filters": {...}, "web_search_filters": {...}},
]
```

- **`variable_path`.** A `tools` entry's map is keyed by its tool's `script_id`, an `agent` entry's by the child agent id. These entries own their keys. The bridge-level `variables_path` (folder merge, legacy data) fills only keys that no entry covers, so a mapping removed in the new format does not come back from stale data. A request `variables_path` still replaces the whole result.
- **Pre-tools.** For a `custom_function` entry, `script_id`, `url` and `title` come from the entry, its `variable_path`, or its joined `apicalls` doc. `config.params` is that doc's `fields` keys. Any other `pre_tool_type` uses `variable_path` as its `config`. These pre-tools are placed ahead of the legacy `pre_tools` list, which also holds the folder pre-tool.
- **Post-tool.** A `post_tool` entry beats the folder's post-tool and is resolved the same way. Without one, the legacy `post_tool` field is used, and the folder's post-tool replaces it.
- **Legacy fields no longer read:** `function_ids` (tools), `doc_ids` (RAG), and the top-level `built_in_tools`, `web_search_filters` and `gtwy_web_search_filters`. Still read: `pre_tools`, `post_tool` and `connected_agents` (child configs only, see Gotchas).

### Loading the agent (`get_bridges_with_tools_and_apikeys`)

- Source: `configuration_versions` when `version_id` is given, else `configurations`. Both match `org_id`, so the org id must be the same string Mongo stores.
- `environment` (without `version_id`) reads `settings.environment_config.{environment}`. If that holds a version id, the pipeline swaps to that version doc and sets `parent_id`. `getConfiguration` then treats it as that version.
- When a version is loaded, agent-level fields it lacks (`bridgeType`, `bridge_status`, `name`, `bridge_limit*`, `bridge_summary`, ...) are filled from the parent agent doc.
- The aggregation joins `apicalls` (into `apiCalls` for `connected_tools` entries of type `tools`, and into `pre_tools_data` for `pre_tool` / `post_tool` entries), `apikeycredentials` (keys with limits and status), `configurations` (`agent_info` and names of the `agent` entries) and `rich_ui_templates`. Legacy `pre_tools` entries are objects, so the id `$convert` turns them into `null`: they are not joined and get no `title` from `apicalls`. Reviewer tools are no longer joined (see Gotchas).
- A second aggregation on `folders` (when `folder_id` is set) merges in folder keys, folder tools (minus those turned off in `embed_override.tools`), the folder pre-tool, the post-tool (`script_id`, `url` and `title` from its `apicalls` doc; legacy folders store only `config.post_tool_id`), `variables_path`, prompt, a `json_schema` response type, folder limits and `wrapper_id`. The folder post-tool is skipped when the agent has a `post_tool` entry in `connected_tools`.
- Cache: `cd_bridge_data_with_tools_{org_id}_{bridge|version}_{id}`, plus `_env_{environment}` whenever `environment` is passed without `version_id`. Default TTL is 2 days. It is written with `store_in_cache_with_tags`, and `extract_cache_tags` registers `tag:agent:`, `tag:version:`, `tag:tool:`, `tag:apikey:`, `tag:folder:`, `tag:connected_agent:`, `tag:wrapper:` and `tag:rag:` tags. When one of those entities changes, a Mongoose plugin in gtwy-node deletes every cached blob registered under its tag. Writes that skip Mongoose hooks (raw driver calls, bulk writes, writes from this repo) purge nothing.

### The direct agent (`POST /api/v2/model/ai/completion`)

`direct_auth_and_rate_limit` runs `jwt_middleware`, rate-limits by org, validates `DirectCompletionRequest` (needs `service` and `configuration.model`, and rejects agent and version ids), then:

1. `fill_model_defaults` sets every model parameter the caller left out to `"default"`, as Node does for a new agent.
2. `get_or_create_direct_agent` returns the org's hidden agent id. It is cached for 30 days in `cd_direct_agent_{org_id}`. On a miss it runs an upsert on `{org_id, slugName: "__gtwy_direct_api__", bridgeType: "direct"}` that also clears `deletedAt`, and retries once on `DuplicateKeyError`.
3. The id is written into the cached body as `bridge_id`, and `request.state.is_direct_call` is set. The request then goes through `add_configuration_data_to_body(CompletionRequest)` and `chat_completion` like any agent call. The empty agent config (`{"type": "chat", "prompt": ""}`) is overlaid by the body's `configuration`.

gtwy-node knows this agent too: it refuses to delete it and leaves it out of agent lists (`bridgeType: { $ne: "direct" }`).

## Contracts

`getConfiguration` returns either an error dict (`{"success": False, "error": "..."}`) or:

```python
{"success": True,
 "primary_bridge_id": "<agent id; parent id when only a version was given>",
 "bridge_configurations": {"<agent id>": cfg, "<child agent id>": cfg, ...}}
# after the middleware: db_config["billing_attribution"] = {user_id, folder_id, is_embed}
# and, when the wallet is needed, db_config["org_billing_plan"]
```

One `cfg` (built in `_prepare_configuration_response`, trimmed):

```python
{"configuration": {"model", "prompt": str, "tools": [...], "tool_choice", "type", "<params>": value|"default"|"min"|"max"},
 "service": "openai", "apikey": "<decrypted>", "service_apikeys": {"<service>": "<decrypted>"},
 "is_request_apikey": bool, "apikey_object_id": {"<service>": "<cred id>"}, "apikey_status": {...},
 "bridge_id": "<parent_id or _id>", "version_id": "<version or published_version_id>",
 "tool_id_and_name_mapping": {"<tool name>": {"url", "headers", "name", "method"} | {"type": "RAG"|"AGENT"|"Gtwy_Web_Search"|...}},
 "pre_tools_data": [{"_type", "config", "args", "title"}],   # connected_tools pre-tools first
 #   custom_function config from connected_tools: {"function_id", "script_id", "url", "required", "params"}
 "post_tool_data": {"script_id", "url", "args", "_id", "title"} | None,
 "variables": {..., "current_time_date_and_current_identifier": str}, "variables_path": {...},
 "settings": {...},            # agent settings; settings.fall_back.apikey is DECRYPTED when set
 "rag_data": [<connected_tools "docs" entries>], "gpt_memory", "gpt_memory_context",
 "built_in_tools", "web_search_filters",
 "user_id", "folder_id", "is_embed", "wrapper_id", "chatbot", "cache_on", "auto_model_select",
 "reviewer_agent", "reviewer_prompt",
 "reviewer_tools": [],         # always empty now, see Gotchas
 "richui_templates", "meta", "name", "org_name",
 "api_collection": {...}, "limit": {"bridge": {...}, "folder": {...}, "apikey": {...}},
 # added by reserve_credits_and_api_key_setup:
 "wallet": bool, "org_billing_plan": "<plan>"}
```

- `add_configuration_data_to_body` returns `db_config` and changes the cached request JSON in place. Downstream code relies on reading the merged body through `request.json()`.
- After `jwt_middleware`, `request.state.profile["org"]["id"]` is a string. `send_data_middleware` and `add_configuration_data_to_body` read it directly.
- A non-`None` `credit_hold_token` means the request owes exactly one `release_credits`. The middleware releases it on its own failures. After that, the handler or the queue consumer releases it.
- `get_bridges_with_tools_and_apikeys` raises `BadRequestException("Invalid Bridge ID provided")` for an id that is not an ObjectId. The middleware turns that into 400 `"Error in getting data: ..."`.

## Gotchas

> [!WARNING]
> `body.update(primary_config)` lets the agent's values replace the request's. Request `settings` (fall_back, guardrails, ...) are thrown away for normal agent calls; only `settings.response_format` and `wrapper_id` are put back. Guardrails are read from `settings.guardrails`. The top-level `guardrails` field and the `guardrails` argument of `getConfiguration` are never used. The `chatbot` argument is ignored too: it is recomputed from `bridgeType == "chatbot"`.

> [!WARNING]
> The per-agent limit reads only `body.bridge_id`, so callers that send `agent_id` or only `version_id` skip it. The thread key `nd_rate_limit_{thread_id}` is not scoped to an org: two orgs using the same `thread_id` share one 20-per-minute bucket, and so does an org id on the direct route. The counter is a read-then-write, not atomic, so parallel requests can undercount.

> [!WARNING]
> The public chatbot limit is never applied: `get_nested_value` only understands `body.`, `profile.` and `headers.` paths, so `state.profile.limiter_key` resolves to `None`. Also, `combined_auth` catches any `HTTPException` from the first path, including its 429, and retries with `agents_auth`. On `Public_Agents`, both paths accept the same token, so the 429 is swallowed.

> [!WARNING]
> `jwt_middleware` and `agents_auth` decode the `Authorization` header as-is. A `Bearer ` prefix makes it fail. Only `chat_bot_auth` and the OpenAI route strip it. A request with no credentials gets 404 "unauthorized user", not 401. The blacklist check and `is_org_blocked` both let the request through when Redis fails.

> [!WARNING]
> Embed tokens: `create_or_get_user` caches the `createCUsers` reply for 2 days before anything checks it. A 2xx reply without `data.user` turns into 401 "unauthorized user" for that user, org and folder until the key expires. The org record is cached for 2 days too, so a rotated `gtwyAccessToken` is not seen until `embed:org_{org_id}` expires or is deleted. A `SecretKey` token that happens to carry a top-level `folder_id` is sent down the embed path and fails.

> [!WARNING]
> The cached agent blob lives for 2 days. gtwy-node keeps it fresh by purging tags. If you join a new entity into `get_bridges_with_tools_and_apikeys`, also emit its tag in `extract_cache_tags`, and keep `src/configs/constant.py:tag_keys` in sync with gtwy-node's src/configs/tagKeys.js. Otherwise edits to that entity stay stale. `update_bridge` (unused here) deletes only the non-environment key. `extract_cache_tags` still takes `tag:rag:` ids from `doc_ids`, but RAG now reads `connected_tools` `docs` entries, so docs attached only there get no rag tag.

> [!WARNING]
> Folder settings beat agent settings. Folder keys replace agent keys for the same service, the folder's `apikey_object_id` replaces the agent's, and a folder `post_tool` replaces a legacy agent-level `post_tool`. Only a `post_tool` entry in `connected_tools` beats the folder's. The fallback key (`settings.fall_back.apikey`) is looked up only among agent-level keys, and it is stored decrypted inside `settings`. Never log `settings`.

> [!WARNING]
> A paused agent raises instead of returning an error dict, so the caller sees `"Error in getting data: Agent is Currently Paused"`. A connected agent that fails to load (paused, deleted, over its limit, no key) is dropped from `bridge_configurations`, but the parent still lists it as a tool.

> [!WARNING]
> Legacy data half-works. A child that is only in the legacy `connected_agents` map gets a config in `bridge_configurations`, but `add_connected_agents` makes tools only from `connected_tools` entries, so the model cannot call it. Tools in `function_ids` and docs in `doc_ids` are ignored. Tool, pre-tool, post-tool and reviewer URLs come only from stored data (the `apicalls` doc or the entry); no URL is built from `script_id` any more, so a missing `url` sends the call to `None`.

> [!WARNING]
> `reviewer_tools` in every `cfg` is empty. #1752 removed the `apicalls` lookup that filled `reviewer_tools_data`, but `_prepare_configuration_response` still reads it. A webhook reviewer (`settings.review_agent.reviewer_tools` with no reviewer agent or prompt) then fails on round 1 in `_call_tool_reviewer` with "No reviewer_tools configured", and the main answer is returned unreviewed.

> [!TODO] Needs a human: was dropping the reviewer-tools lookup in #1752 intended, or should it come back (or move into `connected_tools`)?

> [!WARNING]
> Any check you add after the credit reservation must raise `HTTPException`, never return early. The `except HTTPException` branch is what releases the hold, and it used to leak there.

> [!WARNING]
> Direct agent: the cached id is trusted for 30 days, and the upsert that un-deletes the doc runs only on a cache miss. Never add a `deletedAt` filter to that query: a second insert hits the unique `(org_id, slugName)` index until Mongo's 30-day TTL removes the deleted doc, and every direct call of that org would fail. Keep every dict field in `_direct_agent_doc` non-`None`, because `getConfiguration` calls `.items()` and `{**x}` on them.

> [!WARNING]
> `Config.PUBLIC_CHATBOT_TOKEN` reads the lowercase env var `public_chatbot_token`. `get_timezone_and_org_name` caches an empty result from a failed MSG91 call for 2 days, and that org then falls back to timezone `+5:30` and an empty org name.

## Why it is built this way

- **One hidden agent per org for direct calls** (PR #1794). Billing, thread memory, streaming and history are all keyed by `bridge_id`. Giving each org one empty `bridgeType: "direct"` agent lets direct calls reuse `chat_completion` unchanged. A new code path was not built.
  - `fill_model_defaults` exists because Anthropic rejects a call without `max_tokens`.
  - The org-level limit exists because `body.bridge_id` is absent there.
  - Request settings are merged in because the agent has none.
  - A soft-deleted direct agent is restored, not replaced, because a replacement would hit the unique index (see Gotchas).
- **Validation order on `/ai/completion`.** `DirectCompletionRequest` must run before the `bridge_id` is injected, since it rejects agent ids. The later `add_configuration_data_to_body(CompletionRequest)` check needs that injected id. Swapping either schema breaks the route.
- **`{bridge|version}` in the cache key** (bb882de2). Versions were saved with the same `_id` values as agents, so without the type in the key a version and an agent could share a cache entry.
- **Environment resolution in the pipeline** (b4fb4cab). An agent can pin a version per environment. The parent-agent lookup that follows exists because agent-level fields such as `bridgeType` live only on the agent doc, not on versions.
- **Embed tokens in this repo** (#1777). `embed_service.py` copies gtwy-node's embed resolution, cache keys and status codes, so an embed token behaves the same whether it reaches gtwy-node or gtwy-ai directly (for example as the SDK's `Authorization` credential).
- **One `connected_tools` list** (#1752, #1781). Tools, agents, pre/post-tools, docs and built-in tools moved into one array. #1781 fixed what the switch broke: post-tools used the `apicalls` id as the `script_id`, and agent calls failed with a `KeyError` because children were read only from `connected_agents`.
- **Tag-based purging** (#1540) replaced deleting single keys. One tool, key or folder edit has to clear every agent blob that embeds it.
- **`strip_gateway_filled_args`.** An argument with a `variables_path` is filled by the gateway from request variables, so the model must never see it. Leaving its name in `required` without a matching property makes the schema invalid.
- **`owner_id` derivation** (70c248c6). Requests through the model router have no `owner_id`, so the middleware derives it the same way the interface middleware does.
- **Public agents use `orgId::slugName`** (#1531). Slugs are unique only per org, so a public user's slug must carry its org.

> [!TODO] Needs a human: is `OPENAI_API_KEY_GPT_5_NANO` in `setup_api_key` still needed now that `reserve_credits_and_api_key_setup` gives every keyless agent a platform key?

## Changing it safely

- **New request field the config should honor.** Add it to `CompletionRequest` in `src/schemas/completion_schemas.py`, pass it from `add_configuration_data_to_body` into `getConfiguration`, and put it on the `cfg` in `_prepare_configuration_response`. Then read it in `parse_request_body` ([chat-orchestration](chat-orchestration.md)). If `body.update(primary_config)` would overwrite it, put the request value back after that line, as is done for `wrapper_id`.
- **New field on the agent doc.** Mongo schemas live in gtwy-node (src/mongoModel there). If versions should inherit it from the parent, add it to the parent-lookup `$addFields` stage. Clear the `cd_bridge_data_with_tools_` keys, or wait for a tag purge, before testing.
- **New built-in tool.** Add the name to `inbuild_tools` in `src/configs/constant.py`, add an `add_*_tool` helper next to `add_browser_tool`, and call it in `_prepare_configuration_response` before `add_connected_agents`. The tool must also be runnable by name in [tool-calling](tool-calling.md).
- **New auth path.** Set `request.state.profile` with a string `org.id` and a `user.id`, and call `is_org_blocked`. Do not put any route behind `add_configuration_data_to_body` unless something releases the credit hold on every exit.
- `getDefaultValue.py`, `generateToken.py`, `get_bridges_with_redis`, `get_bridges_without_tools`, `update_bridge` and `get_advanced_param_keys` have no callers. Do not extend them on the assumption that they run.
- There are no automated tests in this repo. To check a change, call `POST /api/v2/model/chat/completion` with a real agent (try with and without `version_id` and `environment`), then `POST /api/v2/model/ai/completion` and `POST /chatbot/{botId}/sendMessage`. Watch the 400 bodies and that the credit hold is released.

Related docs: [billing-and-limits](billing-and-limits.md) (credit reservation, spend limits, blocked orgs), [api](../api.md), [environment](../environment.md), [data model](../data-model.md) (collections and Redis keys), [testcases](testcases.md) (also calls `getConfiguration`), [reviewer-and-guardrails](reviewer-and-guardrails.md), [media-rag-chatbot](media-rag-chatbot.md) (chatbot request schema).
