# Plan: Serve GTWY Agent Tools via MCP Server-Mode (Redis Bridge)

## Context

Today GTWY runs the tool loop itself: it sends tool definitions to the LLM, the provider returns a `tool_call`, GTWY executes it (mostly HTTP via `axios_work`), injects the result, and re-calls the provider — N tool calls ≈ N+1 gtwy↔provider round-trips, re-sending the growing message history each turn.

The goal is to cut that round-trip overhead (latency) for eligible agents by switching to **MCP server-mode**: GTWY passes an MCP server URL to the provider, and the provider runs the entire tool loop itself against that MCP server. GTWY already has the provider-side wiring for this (`server_mcp_config`, `resolve_mcp_type`, `extract_server_side_mcp_calls`) — it is currently used only for external, user-attached MCP servers. This plan turns an agent's own HTTP tools into an MCP server that the existing `viasocket-mcp` service hosts, reading tool definitions and runtime data from a shared Redis namespace.

The hard part is **not** passing the URL — it is preserving GTWY's `variables_path` behaviour (hide some params from the model + inject their values at execution time), which now must happen inside `viasocket-mcp`. We carry the static `variables_path` and the per-request `variables` across to `viasocket-mcp` through Redis.

---

## Scope Decisions (Confirmed)

- **Providers:** all services `server_mcp_config` supports — `openai`, `groq`, `grok`, `mistral`, `anthropic`. Request-key delivery works for all of them via the MCP server headers / Anthropic `authorization_token`.
- **Eligibility:** per-agent all-or-nothing. An agent uses server-mode only if **every** tool is a plain HTTP tool (no RAG / AGENT / client-MCP / Gtwy_Web_Search). Otherwise it stays on the current client-side path, unchanged. Slow tools (> provider MCP timeout) should be left client-side.
- **Non-goals (this iteration):** per-tool hybrid in one request; Gemini (no server-mode support); migrating RAG/AGENT/web-search tools.

---

## Guarantee

The change is **additive and gated**. Agents without the opt-in flag hit zero modified code paths — the existing `function_call` → `run_tool` → `process_data_and_run_tools` → `axios_work` loop runs exactly as today. The `viasocket-mcp` changes are a new route, leaving the existing Mongo-backed `/mcp` and `/sse` flows untouched.

---

## Shared Redis Contract (the bridge between the two repos)

Both services already share one Redis instance with different per-project prefixes. Introduce one neutral, explicitly-namespaced family of keys that both repos hard-agree on (do **NOT** reuse GTWY's internal `AIMIDDLEWARE_<ENV>_` prefix — define a dedicated namespace so the contract is unambiguous):

| Key | Producer | Consumer | Value (JSON) | TTL |
|---|---|---|---|---|
| `gtwy_mcp:{ENV}:tools:{org_id}:{bridge_id}` | GTWY | viasocket-mcp | `{ tools: [ {name, description, properties, required, url, method, headers, query_params} ], variables_path: { <func_name>: { <arg_path>: <variable_path> } } }` | 2 days (refreshed on bridge cache build) |
| `gtwy_mcp:{ENV}:vars:{request_token}` | GTWY | viasocket-mcp | `{ org_id, bridge_id, message_id, variables: {...} }` | ~5 min (must exceed max provider response + tool exec time) |

- `{ENV}` = deployment environment (must match on both sides).
- `request_token` = opaque per-request UUID minted by GTWY.
- The tools bundle drives `tools/list` (schema, after filtering) and execution config (url/method/headers/query_params).
- The vars entry drives both the `tools/list` field-hiding (a field is hidden only when its variables value is present) and `tools/call` injection.

> 📌 Document this contract in both repos (e.g. a short `docs/gtwy-mcp-redis.md`).

---

## GTWY Changes (AI-middleware-python)

### 1. Write the tools bundle to shared Redis

**File:** `src/db_services/ConfigurationServices.py` (~L125–133, where `cd_bridge_data_with_tools_` is set) + helper in `src/services/utils/getConfiguration_utils.py`.

When the bridge-with-tools cache is (re)built, also assemble and write the `gtwy_mcp:{ENV}:tools:{org}:{bridge}` bundle. Reuse existing data:

- execution config (url, method, headers, query_params) from `tool_id_and_name_mapping` (built at `getConfiguration_utils.py:106–111, 166–172`),
- properties / required / description from `configuration["tools"]`,
- `variables_path` from the bridge configuration.

Only include tools whose type is plain HTTP (skip RAG/AGENT/MCP/web-search).

### 2. Per-agent opt-in + server-mode resolution

**Files:** `src/services/utils/mcp_utils.py` (`resolve_mcp_type`, L163), `baseService.py` (`service_formatter`, L455–522).

- Add a bridge-config flag (e.g. `configuration.mcp_self_serve: true`).
- Make `mcp_type` honor a per-agent override: server-mode only when `mcp_self_serve` is set **and** service is in the supported set **and** the agent passes the eligibility check (below). Otherwise fall back to the existing `resolve_mcp_type` model lookup.
- Keep the existing `server_mcp_config` serializer as-is (it already emits headers / Anthropic `authorization_token`).

### 3. Eligibility check (all-or-nothing)

**File:** new helper in `src/services/utils/mcp_utils.py`.

`is_agent_server_eligible(tool_id_and_name_mapping)` → `True` only if every entry is plain HTTP (type not in `{RAG, AGENT, MCP, Gtwy_Web_Search}`). If any tool is ineligible, the agent silently stays on the client path.

### 4. Mint request token + write per-request variables + build mcp_config

**File:** `baseService.py` — new async `prepare_mcp_server_mode(self, service)`.

Because `service_formatter` is sync and reads `self.configuration["mcp_config"]`, do the async work **before** the first `service_formatter` call — i.e. at the start of `chats()` / `stream()`, guarded by a once-flag so the (single) server-mode request doesn't rewrite. Steps:

1. If not eligible / flag off / unsupported service → return (no-op).
2. Mint `request_token = uuid4()`.
3. `store_in_cache("gtwy_mcp:{ENV}:vars:{token}", {org_id, bridge_id, message_id, variables}, ttl=300)` (reuse `cache_service.store_in_cache`).
4. Build `self.configuration["mcp_config"] = {"servers": [{"name": "gtwy", "url": "<viasocket-mcp>/gtwy/mcp/{bridge_id}-{org_id}", "headers": {"Authorization": f"Bearer {token}"}}]}`.
5. Ensure the agent's own tools are **not** also passed inline: in server-mode, leave `configuration["tools"]` empty so `tool_call_formatter` is skipped (`baseService.py:461`) and only the MCP server is attached.

### 5. Logging

No new code required for the happy path: `extract_server_side_mcp_calls` (`mcp_utils.py:44`) is already merged into history at `prepare_history_params`. Note `flowHitId` and per-tool latency are **not** available for provider-executed calls — acceptable for the pilot.

---

## viasocket-mcp Changes (viasocket-mcp)

> All new code paths; existing Mongo-backed `/mcp` and `/sse` flows untouched.

### 1. Redis client

- Add `ioredis` to `package.json`.
- New `src/service/redis-config.ts` mirroring `mongodb-config.ts`: connect from `process.env.REDIS_URI` (same instance GTWY uses). `connectRedis()` called in `index.ts`.

### 2. Shared-key reader

New `src/service/gtwyToolSource.ts`:

- `getToolBundle(bridgeId, orgId)` → reads `gtwy_mcp:{ENV}:tools:{org}:{bridge}`.
- `getRequestContext(token)` → reads `gtwy_mcp:{ENV}:vars:{token}`.

### 3. New route + controller

- New `src/routes/gtwyMcpRoutes.ts` mounted at `/gtwy/mcp` in `src/index.ts` (alongside existing mounts at L25–29). Support POST (Streamable HTTP) and GET/SSE, mirroring `mcpController`/`sseController`.
- New `src/controller/gtwyMcpController.ts`: parse `:id` as `{bridgeId}-{orgId}`, extract `request_token` from `Authorization: Bearer` header (same extraction as `mcpRoutes.ts:16`), then build the server via the Redis register below.

### 4. Redis-backed tool registration (with filter + inject)

New `src/service/gtwyToolRegister.ts`, modeled on `registerToolsOnServer`:

1. Load bundle (by bridge/org) + request context (by token).
2. For each tool, build the Zod schema reusing `jsonSchemaToZod`/`createToolSchema` (`sseService.ts:182`), then filter out fields per a TS port of `apply_variable_path_filters` (drop keys whose `variables_path` value resolves in `variables`; also drop from `required`).
3. Register `server.tool(name, description, shape, handler)`. The handler:
   - maps param names back (existing logic),
   - injects hidden values per a TS port of `replace_variables_in_args` (set nested `variables_path` values into args),
   - calls the generalized HTTP executor.

### 5. Generalized HTTP executor

Generalize `genericExecute` (currently hardcoded to `flow.sokt.io/func/{flowId}`) into `httpExecute(httpConfig, args)` that mirrors GTWY's `axios_work` + `resolve_url_params` (`utils.py:131–188`): substitute `:param`/`{param}` in the URL, send GET args as query params, send `query_params`-listed keys as params and the rest as JSON body, apply headers. Keep the old `genericExecute` signature for the existing Mongo flow.

---

## Ports to Keep Behaviour-Identical

The two TS ports must match the Python semantics exactly:

- filter ⇔ `apply_variable_path_filters` (`utils.py:45`)
- inject ⇔ `replace_variables_in_args` (`baseService.py:829`)
- url/param resolution ⇔ `resolve_url_params` (`utils.py:131`)

---

## Risks / Notes

- **Secrets in Redis:** `variables` and tool headers may contain auth tokens. They live in a shared Redis with short TTL — acceptable if Redis is internal/trusted. Confirm before rollout.
- **Provider tool-call timeout:** server-mode makes the provider wait synchronously on each tool. Keep slow tools (your 100s example — already capped at 60s by `pre_function`) client-side. Confirm exact per-provider MCP timeout against current provider docs before enabling.
- **tools/list caching:** some providers cache the MCP tool list; the filtered schema must be stable per agent (it is, as long as the agent always supplies the same hidden variables).
- **MCP URL** must be publicly reachable by the provider's servers.

---

## Verification

- **Unit (viasocket-mcp):** tests for the filter, inject, and url/param ports using fixtures copied from GTWY behaviour (same `variables_path` + `variables` → identical hidden fields and identical final HTTP request).
- **Contract:** a script that writes a sample tools bundle + vars to Redis and asserts `getToolBundle`/`getRequestContext` read them back; verify GTWY's writer produces the exact same shape.
- **E2E happy path:** configure a test agent with 2–3 plain HTTP tools and a `variables_path` that hides one auth field; set `mcp_self_serve`; send a chat on an Anthropic model and on one OpenAI-family model. Confirm: provider hits `/gtwy/mcp/...`, `tools/list` shows the filtered schema, the tool executes with the injected value, the final answer returns, and history shows the server-side MCP calls.
- **Regression:** an agent without the flag → byte-for-byte the current client-side loop (spot-check logs); an existing Mongo-backed viasocket-mcp URL still works.
- **Latency measurement:** on a 3+ sequential-tool conversation, compare end-to-end latency client-mode vs server-mode to confirm the round-trip win (and watch for the parallel-tool regression case).
