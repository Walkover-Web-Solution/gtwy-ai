---
covers: ["src/configs/constant.py", "src/services/utils/getConfiguration.py", "src/services/commonServices/common.py", "src/services/utils/common_utils.py", "src/services/billing/billing_utils.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Glossary

Terms that mean something specific here, or that the code spells differently from the UI.

## Agents and requests

| Term | Meaning | In code |
|---|---|---|
| bridge = agent | The same thing. The code says bridge; the UI and newer routes say agent. An agent is a document in Mongo `configurations`. | `bridge_id`, `src/db_services/ConfigurationServices.py:configurationModel` |
| version | A draft or published copy of an agent in `configuration_versions` (`parent_id` = agent id). Publishing (done in gtwy-node) copies a version over the agent. A call with only `bridge_id` runs the published config. | `version_id` |
| environment | A key in the agent's `settings.environment_config` that pins a version id | `get_bridges_with_tools_and_apikeys` |
| direct agent | Hidden per-org agent (`bridgeType: "direct"`, slug `__gtwy_direct_api__`) behind `POST /api/v2/model/ai/completion` | `src/services/direct_agent_service.py:DIRECT_AGENT_SLUG` |
| embed token | A JWT the customer signs with their org's `gtwyAccessToken`. It has top-level `org_id`, `folder_id` and `user_id` (or `unique_identifier`). The top-level `folder_id` is how `jwt_middleware` tells it apart from a normal user JWT. | `src/services/proxy/embed_service.py:resolve_gtwy_embed_token` |
| connected_tools | The agent field that now lists everything attached to an agent (tools, agents, pre-tool, post-tool, docs, built-in tools), replacing the legacy `function_ids` / `connected_agents` / `doc_ids` / `built_in_tools` fields | `src/db_services/ConfigurationServices.py:get_bridges_with_tools_and_apikeys` |
| bridge_configurations | Map of agent id → fully resolved config for the primary, connected and reviewer agents of one request | built by `src/services/utils/getConfiguration.py:getConfiguration` |
| primary_bridge_id | The agent the request is for (the parent id when only a version was given) | `chat_multiple_agents` |
| connected agent / agent-as-tool | Another agent exposed to the model as a tool; it runs in-process through `chat()` | `add_connected_agents`, `src/services/utils/ai_call_util.py:call_gtwy_agent` |
| transfer | A tool call with `action_type == "transfer"` that hands the turn to another agent | `src/services/utils/transfer_handler.py:check_transfer_from_codes_mapping` |
| sticky agent | Redis memory of which agent answered a thread last, so the next turn starts there | `last_transffered_agent_` key in `redis_keys` |
| parsed_data | The flat per-request dict built by `parse_request_body` and changed by every later step | `src/services/utils/common_utils.py:parse_request_body` |
| playground | A request from the GTWY UI test panel: no alerts, and non-stream answers are delivered over RTLayer | `is_playground` |
| pre-tool | A tool run **before** the LLM (custom function, query refiner, RAG, web search) whose output fills a prompt variable | `handle_pre_tools` |
| post_tool | A folder-level viaSocket function run **after** the LLM; it can replace the answer | `handle_post_tool` |
| wrapper | A prompt template wrapped around the agent's prompt | `apply_prompt_wrapper` |
| plan mode / todo mode | Request-body `mode: "plan"`: planner → per-task workers → synthesizer | [modules/todo-mode.md](modules/todo-mode.md) |
| review round | One reviewer call plus an optional re-run of the main agent | `src/services/commonServices/reviewer_service.py:run_review_loop` |

## Providers and tools

| Term | Meaning | In code |
|---|---|---|
| service | A provider key such as `openai` or `anthropic`. The live list is the Mongo `services` collection; `service_name` in `src/configs/constant.py` must also hold the key. | `src/configs/service_registry.py:service_registry_document` |
| wire_format | The request/response shape family of a service (`openai_chat`, `openai_responses`, `anthropic`, `gemini`, …) | `WireFormat` in `src/configs/constant.py` |
| client | Which SDK makes the call. Only `openai_sdk` changes routing. | `Client` in `src/configs/constant.py`, `uses_openai_sdk` |
| Call class / runner | A `BaseService` subclass per service with `execute()`, plus the single-call function it uses | e.g. `OpenAICompatibleHandler` |
| service_keys | Maps GTWY parameter names (e.g. `creativity_level`) to each provider's names | `src/configs/serviceKeys.py` |
| finish_reason | Normalized to `completed`, `truncated`, `tool_call` or `other` | `src/services/utils/formatters/finish_reason.py:finish_reason_mapping` |
| codes_mapping | Tool calls parsed from a model reply: `{call_id: {name, args, error}}` | `make_code_mapping_by_service` |
| tool_id_and_name_mapping | Model-visible tool name → how to run it (HTTP, RAG, AGENT, MCP, built-in) | `src/services/utils/getConfiguration_utils.py:setup_tools` |
| tools_call_data | The tool calls saved on the history row. gtwy-node queries its keys directly. | `prepare_history_params` |
| built-in tool | Gateway-run tools `Gtwy_Web_Search` (Firecrawl) and `Gtwy_Browser` (Steel) | `inbuild_tools` in `src/configs/constant.py` |
| client MCP / server MCP | Whether this gateway calls the MCP tools itself, or the provider calls them | `src/services/utils/mcp_utils.py:resolve_mcp_type` |

## Billing

| Term | Meaning | In code |
|---|---|---|
| wallet (flag) | This agent runs on GTWY's platform key, and its usage is charged to the org's Lago wallet | `reserve_credits_and_api_key_setup` |
| platform key | GTWY's own provider key, from Mongo `platform_apikeys` | `get_platform_apikey` |
| shadow balance | A Redis copy of the org's wallet balance, used to admit requests without calling Lago | `src/services/billing/billing_utils.py` |
| hold / hold token | A flat credit reservation taken before the call and released exactly once afterwards | `credit_hold_token`, `release_credits` |
| plan | `free` or `paid` (shown as "Pro"): a model allowlist and per-hit fees | `src/configs/plan_registry.py` |
| hit fee | A flat fee per request on some plans | `build_hit_fee_event` |

## Storage

| Term | Meaning | In code |
|---|---|---|
| `cd_` / `nd_` | Redis key prefixes: **c**an **d**elete (rebuildable) vs **n**o **d**elete (Redis is the only copy, or a counter) | `redis_keys` in `src/configs/constant.py` |
| `REDIS_PREFIX` | `AIMIDDLEWARE_<ENVIRONMENT>_`, added to every key by the cache helpers | `src/services/cache_service.py` |
| main / log / metrics queue | Request queue (consumed here); history and metrics queues (consumed by gtwy-node) | `queue_obj`, `sub_queue_obj`, `metrics_queue_obj` |

## Id spaces

- **`org_id`** is always a string in this repo. Mongo matches on the string, and `jwt_middleware` converts it.
- **`bridge_id`, `version_id` and `agent_id`:**
  - `agent_id` is the newer name for `bridge_id`.
  - `version_id` points into `configuration_versions`.
  - Code that builds cache keys uses `version_id or bridge_id`. Check which one a key uses before reading it.
- **`thread_id` / `sub_thread_id`:** the conversation and the branch within it. When `sub_thread_id` is missing, the code uses `thread_id`.
- **`message_id`:** one id per request turn. It keys the tool budget (`{bridge_id}-{message_id}`), the hit-fee transaction and batch items.
- **`owner_id`:** `{org_id}_{folder_id}_{user_id}` or just `org_id`. It scopes RAG.
