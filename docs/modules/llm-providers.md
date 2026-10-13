---
covers: ["src/services/commonServices/openAI/**", "src/services/commonServices/anthropic/**", "src/services/commonServices/Google/**", "src/services/commonServices/groq/**", "src/services/commonServices/grok/**", "src/services/commonServices/Mistral/**", "src/services/commonServices/deepgram/**", "src/services/commonServices/deepseek/**", "src/services/commonServices/typesafe/**", "src/services/commonServices/openaiCompatible/**", "src/configs/service_registry.py", "src/configs/model_configuration.py", "src/configs/serviceKeys.py", "src/services/utils/formatters/**", "src/services/utils/formatter/**", "src/services/utils/ai_middleware_format.py", "src/services/utils/token_calculation.py", "src/services/utils/*_token_utils.py", "src/services/utils/load_model_configs.py", "src/services/utils/load_service_configs.py", "src/services/utils/service_config_utils.py", "src/services/utils/helper.py", "src/services/auto_router_service.py", "src/services/utils/auto_router_utils.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# LLM providers

Everything that talks to a model provider: one handler class per service (the "Call" side), one runner per service (the "ModelRun" side), the two Mongo-backed registries that say which services and models exist, the formatters that turn every provider reply into one response shape, token and cost math, and the NotDiamond auto model router. The chat pipeline in `src/services/commonServices/common.py` calls in here; see [chat-orchestration](chat-orchestration.md). Batch handlers (`src/services/commonServices/*/*_batch.py`, `src/services/commonServices/*/*_run_batch.py`) live in the same folders but belong to [batch-api](batch-api.md).

The supported services are the documents with status 1 in the Mongo `services` collection, loaded into `service_registry_document` by `src/configs/service_registry.py:init_service_registry`. Names known to code are the keys of `service_name` in `src/configs/constant.py`. Read those; do not trust any list in prose.

| File | Role |
|---|---|
| `src/configs/service_registry.py` | In-memory copy of the `services` collection plus named capability predicates (`uses_openai_sdk`, `has_openai_choices_shape`, ...). Change-stream refresh. |
| `src/configs/model_configuration.py` | `model_config_document` (service → model → doc) and `platform_apikey_document` (service → decrypted platform key). Change-stream refresh. |
| `src/services/utils/load_service_configs.py`, `src/services/utils/load_model_configs.py` | The Mongo reads behind the two registries. |
| `src/configs/serviceKeys.py` | `get_service_keys` (param rename map from the service doc) and `model_config_change` ("default"/"min"/"max" values). |
| `src/services/utils/helper.py` | `Helper.create_service_handler` (service → Call class). Also a grab-bag: `Helper.encrypt`/`decrypt`, `replace_variables_in_prompt`, rerun queue builders, and several methods with no callers. |
| `src/services/commonServices/*/*Call.py`, `src/services/commonServices/*/*_call.py`, `src/services/commonServices/openAI/openai_response.py` | Call classes: subclass `BaseService`, build the provider request, run the tool loop, format. |
| `src/services/commonServices/*/*ModelRun.py`, `src/services/commonServices/*/*_modelrun.py`, `src/services/commonServices/Mistral/mistral_model_run.py`, `src/services/commonServices/openAI/runModel.py` | Runners: one provider HTTP/SDK call (`*_runmodel`) and a streaming generator (`*_stream`). |
| `src/services/commonServices/openaiCompatible/` | One handler and one runner for every service whose doc says `client` openai_sdk and `wire_format` openai_chat. |
| `src/services/utils/ai_middleware_format.py` | `Response_formatter` dispatcher, request-model stamping, batch result formatting. |
| `src/services/utils/formatters/` | One formatter per response family plus `finish_reason_mapping` and `extract_web_search_annotations`. |
| `src/services/utils/token_calculation.py` | `TokenCalculator`: per-call usage extraction, accumulated usage, cost from model-doc pricing. |
| `src/services/utils/openai_token_utils.py`, `src/services/utils/gemini_token_utils.py` | Usage and cost for image-generation models. |
| `src/services/utils/service_config_utils.py` | `tool_choice_function_name_formatter`: per-wire-format `tool_choice` shape. |
| `src/services/auto_router_service.py`, `src/services/utils/auto_router_utils.py` | NotDiamond model router and its 24h supported-model refresh. |
| `src/services/utils/formatter/ai_middleware_chat_api.py` | Not a formatter: three prebuilt-agent helpers with no importers anywhere in the repo. |

## How it works

### From service key to handler

1. The request (or the stored agent) carries `service` and `model`. `src/middlewares/getDataUsingBridgeId.py` rejects the request unless `model_config_document[service][model]` exists (see [agent-configuration](agent-configuration.md)).
2. If `auto_model_select` is set, `apply_auto_model_selection` may replace both (see [Auto model router](#auto-model-router)).
3. `src/services/commonServices/common.py` builds params with a fresh `TokenCalculator` and calls `src/services/utils/helper.py:create_service_handler`. It checks in this order: openai, gemini, anthropic, groq, grok, deepseek, typesafe by name; then `uses_openai_sdk(service)` → `OpenAICompatibleHandler`; then mistral, deepgram by name; otherwise `ValueError("Unsupported service: …")`.
4. `class_obj.execute()` builds the provider payload and calls `self.chats(...)` or, when `customConfig["stream"] is True`, `self.stream(...)`. Both live in `src/services/commonServices/baseService/baseService.py` and pick the runner with their own if-chain: openai, anthropic, groq, grok by name, then `uses_openai_sdk`, then mistral, gemini, deepgram, typesafe (stream: no deepgram/typesafe). Dispatch, the tool loop and `service_formatter` are documented in [tool-calling](tool-calling.md).
5. Every non-streaming runner wraps its single call in `src/services/commonServices/api_executor.py:execute_api_call`, which times it and calls `token_calculator.calculate_usage(response)` on success.
6. The Call class passes the raw reply (after any tool loop) to `Response_formatter` and returns `{"success", "modelResponse", "historyParams", "response"}`.

`deepseek` is the one split case: it has its own Call class (`Deepseek`, its own conversation builder) but no runner of its own; its `services` doc says `client` openai_sdk, so `chats` routes it through the generic `uses_openai_sdk` runner. That is why it sits before the `uses_openai_sdk` branch in `create_service_handler`.

### The `services` collection drives behaviour

`init_service_registry` loads every `services` document with status 1 into `service_registry_document` at startup (`index.py:lifespan`), and `background_listen_for_service_changes` reloads the whole thing on any insert/update/replace/delete and posts `service_registry_updated` to the RTLayer channel `global_model_updates`. The collection's schema is owned by gtwy-node (`Service.model.js`); this repo only reads it.

```jsonc
// services document (trimmed; values are an example)
{
  "service_name": "open_router",
  "status": 1,
  "wire_format": "openai_chat",          // openai_chat | openai_responses | anthropic | gemini | deepgram | typesafe
  "client": "openai_sdk",                // which SDK makes the call
  "base_url": "https://openrouter.ai/api/v1",
  "prompt_role": "developer",
  "supports_tool_calls": true,
  "supports_stream_usage": false,
  "supports_reasoning": false,
  "reasoning_param_style": null,         // summary_flag | thinking_config | output_config_effort | reasoning_effort | reasoning_effort_extra_body
  "extra_body": {}, "reasoning_extra_body": {},
  "apikey_status_codes": { "invalid": [401], "unauthorized": [403], "limited": [402, 429] },
  "service_keys": { "default": { "creativity_level": "temperature", "max_tokens": "max_tokens", "response_type": "response_format" } }
}
```

| Field | Read by | Effect |
|---|---|---|
| `wire_format` | `has_openai_choices_shape`, `has_openai_responses_shape`, `has_anthropic_shape`, `has_gemini_shape`, `uses_string_tool_choice` | Which formatter, tool-call parser, streamed-response rebuild (`build_accumulated_response`), `tool_choice` shape and cache-write carve-out apply. Values are `WireFormat` in `src/configs/constant.py`. |
| `client` | `uses_openai_sdk` (true only for openai_sdk + openai_chat) | Sends the service to `OpenAICompatibleHandler` and the generic runner. Values are `Client` in `src/configs/constant.py`. |
| `base_url` | `openai_compatible_modelrun`, `openai_compatible_stream`, `typesafe_runmodel` | Endpoint. No other runner reads it. |
| `prompt_role` | `OpenAICompatibleHandler` | Role of the system prompt message (default "system"). |
| `supports_stream_usage`, `supports_reasoning` | `openai_compatible_stream` | Adds `stream_options.include_usage`; emits reasoning deltas. |
| `supports_tool_calls` | `BaseService.update_model_response` | Whether tool-loop results are copied back into the response. |
| `reasoning_param_style`, `reasoning_extra_body` | `reasoning_formatter` in `src/services/commonServices/baseService/utils.py` | How a `reasoning` config becomes provider params. None = reasoning ignored. |
| `extra_body` | `BaseService.service_formatter` | Merged into every request's `extra_body`. |
| `apikey_status_codes` | `src/services/utils/api_key_status_helper.py:get_api_key_status` | Maps provider HTTP codes to key status (see [billing-and-limits](billing-and-limits.md)). |
| `service_keys` | `get_service_keys` → `service_formatter` | Renames generic GTWY params (`creativity_level`, `response_type`, `max_tokens`…) to the provider's names, per request type with a "default" map. |
| `supports_streaming`, `default_model` | accessors only | No Python code calls them. Streaming is decided by the model doc's `stream` param and the request. |

### The `modelconfigurations` collection

`init_model_configuration` loads every model document with status 1 into `model_config_document[service][model_name]`; `background_listen_for_changes` reloads on change and posts `model_config_updated` to `global_model_updates`. The same init also loads `platform_apikeys` (decrypted with `Helper.decrypt`) into `platform_apikey_document`, which the wallet gate reads via `get_platform_apikey` ([billing-and-limits](billing-and-limits.md)).

```jsonc
// modelconfigurations document (trimmed, from the TypeSafe migration in gtwy-node)
{
  "service": "typesafe", "model_name": "jev-latest", "status": 1,
  "org_id": "<optional: model visible to this org only>",
  "configuration": {                     // request params the model accepts
    "model":     { "field": "drop", "default": "jev-latest", "level": 1 },
    "questions": { "field": "json", "default": {}, "level": 0 }   // level 0 = only when the request sets it
  },
  "validationConfig": { "type": "chat", "tools": false, "vision": false, "context_window": 64000 },
  "outputConfig": {
    "message": "answers", "tools": "",   // pydash paths used by update_model_response
    "usage": [{ "total_cost": { "input_cost": 0.042, "output_cost": 0, "cached_cost": 0 } }]   // USD per 1M tokens
  }
}
```

A request param whose value is the string "default", "min" or "max" is resolved against the model doc by `model_config_change` ("default" deletes the key, except Anthropic `max_tokens`, which the API requires).

### Call vs ModelRun

- A Call class (`OpenaiResponse`, `Anthropic`, `GeminiHandler`, `Groq`, `Grok`, `Deepseek`, `Mistral`, `Deepgram`, `TypeSafe`, `OpenAICompatibleHandler`) inherits `BaseService`. It owns everything provider-shaped about the request: conversation format (from `ConversationService` in `src/services/commonServices/createConversations.py`), where the system prompt goes, how images/files/audio are attached, built-in tools. It calls `self.service_formatter`, then `self.chats`/`self.stream`, then `self.function_call` if the reply has tool calls, then `Response_formatter` and `self.prepare_history_params`.
- A runner does exactly one provider call. `*_runmodel(configuration, apikey, execution_time_logs, bridge_id, timer, …, service, count, token_calculator)` returns `{"success": True, "response": <raw dict>}` or `{"success": False, "error", "status_code"}`; failures in setup raise `ApiCallError`. `*_stream(configuration, apikey)` is an async generator of delta dicts (shape under Contracts); its last yield carries the tool calls, usage and finish reason.

### Unified response shape

`src/services/utils/ai_middleware_format.py:Response_formatter` picks the formatter by wire format first, then by name: gemini shape, anthropic shape, openai_responses shape, then `groq`, `deepseek`, `grok` by name, then any other openai_chat service → `format_openai_compatible`, then `deepgram`, `typesafe` by name. After formatting, `_stamp_request_model` overwrites `data.model` with the model the request was sent with (recorded by `set_request_model` in `BaseService.__init__`), because providers echo dated snapshot names back. `finish_reason_mapping` folds every provider stop reason into completed, truncated, tool_call or other.

### Tokens and cost

1. `build_service_params` in `src/services/utils/common_utils.py` creates one `TokenCalculator(service, model_output_config)` per attempt (a fallback attempt gets a new one).
2. After every successful provider call (each tool-loop round, and once per finished stream) `calculate_usage` extracts usage with a per-service `match`. Unknown services fall to `case _`, which reads OpenAI chat usage keys when `has_openai_choices_shape` is true. The result is added to `total_usage`.
3. At the end `common.py` replaces `response.usage` with `get_total_usage()` and, for non-image requests, calls `calculate_total_cost(model, service)`, puts the breakdown in `parsed_data["tokens"]` and the total in `response.usage.cost`.
4. Pricing is `outputConfig.usage[0].total_cost` of the model doc, in USD per 1M tokens: `input_cost`, `output_cost`, `cached_cost`, `caching_read_cost`, `caching_write_cost`, `audio_cost_per_minute` (per minute), optional `long_context_cost` + `long_context_threshold`, optional `cost_multiplier` (Gemini).
5. Rules in `calculate_total_cost`: cached tokens are carved out of input and billed at `cached_cost` when that rate exists; for openai_responses services cache-write tokens are carved out too; Gemini thought tokens are billed at the output rate (other providers already count reasoning inside output); Gemini `cost_multiplier` applies above 200K input tokens; `service_tier` "priority" (OpenAI) doubles the total.
6. Image generation (`type` image) uses `calculate_image_usage` / `calculate_image_cost`, OpenAI and Gemini only.

### Auto model router

`src/services/auto_router_service.py:apply_auto_model_selection` runs inside `chat` in `common.py` before the model config is loaded, when the agent or request sets `auto_model_select` (optionally `{"tradeoff": "quality" | "cost" | "speed"}`, default cost, mapped by `auto_model_tradeoff` in `src/configs/constant.py`).

1. Candidate services are the keys of `parsed_data["service_apikeys"]` (only the request's own service when the request brought its own key) that NotDiamond supports.
2. Candidate models are status-1 docs with `validationConfig.type` "chat" that NotDiamond lists for that provider and, for wallet-billed runs, that `allowed_models_for(plan_code, service)` allows.
3. `AsyncNotDiamond.model_router.select_model` picks one. Provider "google" maps to "gemini" (`PROVIDER_NAME_OVERRIDES`).
4. On success it rewrites `model` and `service` (top level and in `configuration`), switches `apikey` to the org's key for the new service, and sets `wallet` False when that key exists, so the run is not billed twice.

`run_supported_services_refresh_loop` (started in `index.py:lifespan`) fetches NotDiamond's model list at startup and every 24 hours into `SUPPORTED_SERVICES_STORE`.

## Contracts

```python
# Call class
result = await Helper.create_service_handler(params, service)  # -> BaseService subclass
out = await result.execute()
# {"success": True, "modelResponse": <raw provider dict>, "historyParams": {...},
#  "response": <formatted>, "transfer_agent_config"?: {...}}
```

```jsonc
// Response_formatter output for chat (keys vary slightly per formatter, see table below)
{
  "data": {
    "id": "...", "content": "text or null", "reasoning": "optional",
    "model": "model name the request used",          // stamped, not the provider's echo
    "role": "assistant", "finish_reason": "completed | truncated | tool_call | other",
    "tools_data": {}, "images": null, "annotations": [{"type": "url_citation", "url": "..."}],
    "fallback": false, "firstAttemptError": "",
    "image_urls": [],          // openai only
    "answers": {}              // typesafe only; content is the same map as a JSON string
  },
  "usage": { "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0 }  // provider-specific, see Gotchas
}
// response.usage after common.py, chat flow (TokenCalculator.total_usage + cost):
{ "total_tokens", "input_tokens", "output_tokens", "cached_tokens", "cache_read_input_tokens",
  "cache_creation_input_tokens", "reasoning_tokens", "audio_duration_seconds", "audio_duration_minutes", "cost" }
// parsed_data["tokens"] = { "input_cost", "output_cost", "cached_cost", "reasoning_cost",
//                           "cache_read_cost", "cache_creation_cost", "audio_cost", "total_cost" }
```

```python
# every *_stream generator yields
{"content": str|None, "tool_calls": list|None, "usage": dict|None, "finish_reason": str|None, "reasoning": str|None}
# optional keys: "error" (stream failed), "output"/"service_tier"/"incomplete_details" (openai final yield),
# "stream_event_only"/"tool_results" (openai server-side MCP events)
```

OpenAI reports a mid-stream failure inside a 200 SSE stream, as an `error` event or as `response.failed` (usually with null usage). `src/services/commonServices/openAI/runModel.py:openai_response_stream` stops at either event and yields one final chunk with `finish_reason: "error"` and the provider's message in `error`, so the stream no longer looks like an empty success.

## Per-provider quirks

| Service | Call / runner | Transport | Quirks |
|---|---|---|---|
| openai | `OpenaiResponse` / `openai_response_model`, `openai_response_stream` | Raw HTTP to a hard-coded Responses API URL | System prompt as role developer, dropped for `reasoning_model`. Images inlined as data URLs. Adds built-in `web_search` / `image_generation` tools when the model doc has `tools`; generated images are uploaded to GCP. Retries up to twice on "Duplicate item found with id" after `remove_duplicate_ids_from_input`. Also image (`OpenAIImageModel`) and embedding (`OpenaiEmbedding`, via `Helper.embedding_service_handler`). |
| anthropic | `Anthropic` / `anthropic_runmodel`, `anthropic_stream` | anthropic SDK; non-stream calls also use `messages.stream` and run in a thread pool via `asyncio.run` | `Helper.replace_variables_in_prompt` splits the prompt into `prompt_blocks` around variables (`split_prompt_for_anthropic`) and `apply_anthropic_cache_control` marks cache breakpoints, so static prompt parts hit the prompt cache. |
| gemini | `GeminiHandler` / `gemini_modelrun`, `gemini_modelrun_stream` | google-genai SDK | `service_formatter` wraps params in `GenerateContentConfig`. Image and video variants. No embedding formatter. |
| groq | `Groq` / `groq_runmodel`, `groq_stream` | groq SDK | Formatter surfaces `message.reasoning`; cached tokens always 0. |
| grok | `Grok` / `grok_runmodel`, `grok_stream` | Raw HTTP to a hard-coded x.ai URL | Registry `base_url` is ignored. |
| deepseek | `Deepseek` / generic openai-compatible runner | AsyncOpenAI | Own formatter (`reasoning_content`); cached tokens from `prompt_cache_hit_tokens`. |
| mistral | `Mistral` / `mistral_model_run`, `mistral_stream` | mistralai SDK | Accepts audio input parts. `stream` key is stripped before the call. |
| deepgram | `Deepgram` / `deepgram_runmodel` | deepgram SDK | Needs an audio URL (user_urls audio or a URL as the user message). `model_option` is appended to the model name. Usage is audio duration only. |
| typesafe | `TypeSafe` / `typesafe_runmodel` | httpx POST to base_url + `/systemone` | Not chat: sends only model, state (the user message, JSON-parsed when it looks like JSON) and `configuration.questions` (types choice, score, noul). No tools, no streaming. |
| openai_sdk + openai_chat | `OpenAICompatibleHandler` / `openai_compatible_modelrun`, `openai_compatible_stream` | AsyncOpenAI with registry `base_url` | Images sent as base64 data URLs (Moonshot rejects URLs). With `reasoning_model` set, no system prompt and `service_formatter` is skipped. |

## Gotchas

> [!WARNING]
> A transient Mongo error during a refresh empties a registry. `get_service_configs` and `get_model_configurations` return `{}` on error, and `init_service_registry` / `init_model_configuration` clear and replace with that. Until the next change event, every request fails ("model or service does not exist!", "Unsupported service"). Only `init_platform_apikeys` guards against this. The `get_service_configs` docstring still mentions "hardcoded defaults"; they were removed.

> [!WARNING]
> A new OpenAI-compatible service is NOT a pure DB insert, despite the docstring in `openai_compatible_modelrun.py`. Without a code change: `service_formatter` does `service_name[service]` whenever `stream` is in the config, so streaming raises "Service key error"; `_SERVICE_NAMES` in `src/schemas/completion_schemas.py` rejects the service in a request body; and the hard-coded list in `BaseService.update_model_response` leaves tool-loop `tool_calls` stale. See [Changing it safely](#changing-it-safely).

> [!WARNING]
> `Deepgram.execute` reads `self.playground`, which `BaseService` stopped setting in ba837ed1 (2026-05-13). Every Deepgram call raises `AttributeError` after the provider replies, on success and failure alike.

> [!WARNING]
> Only `client` openai_sdk is understood. `Client.OPENAI_COMPLETION_SDK` and `Client.MINIMAX_SDK` exist in `src/configs/constant.py`, but no code branches on them: a service with either value has no Call class and `create_service_handler` raises "Unsupported service".

> [!WARNING]
> `service_keys` must contain a "default" map. `service_formatter` evaluates `service_keys["default"]` eagerly, so a service doc without it fails every request with "Service key error: 'default'".

> [!WARNING]
> In the chat flow, `response.usage` is not the formatter's usage: `common.py` overwrites it with `TokenCalculator.total_usage` (summed over all tool-loop rounds) plus `cost`. Provider-specific usage keys from the formatters (`thoughts_token`, `audio_duration`, ...) survive only in batch results.

> [!WARNING]
> `long_context_threshold` and the Gemini 200K multiplier compare against `total_usage["input_tokens"]`, which sums every tool-loop round. A long tool loop with short prompts can cross the threshold and be billed at the premium rate.

> [!WARNING]
> `Response_formatter` returns `None` for a service no branch matches (a new wire format, or Gemini embeddings). Callers then fail on `result["response"]["usage"]`. Add a branch for any new response family.

> [!WARNING]
> Several imports are lazy on purpose to break import cycles: `send_message` in `_async_change_listener`, `Helper` in `get_platform_apikeys` and in `typesafeCall.py`, the registry predicates inside `token_calculation.py`. Moving them to module level breaks startup.

> [!WARNING]
> The auto router fails silently. With no `NOT_DIAMOND_API_KEY`, an empty NotDiamond model list (the startup fetch failed), no candidates, or a NotDiamond error, it returns and the request runs on the configured model. The refresh loop's docstring says "every 15 days"; the interval is 24 hours.

## Why it is built this way

- **Named capabilities instead of `service == …` lists.** PR #1601 (squashed into f7713ce4, 2026-06-12) added `service_registry.py`. Its docstring gives the reason: replace scattered allow-lists with predicates, and model two separate things, the response shape (`wire_format`) and the SDK that makes the call (`client`). groq, grok and mistral share the OpenAI chat shape but not the client, so they keep their own runners.
- **One handler for OpenAI-compatible services.** The same PR deleted the per-service open_router, neev_cloud, openai_completion and deepseek runners and the openai_completion handler in favour of `src/services/commonServices/openaiCompatible/`, described as "behaviour-preserving": base URL and stream flags come from the registry. It also split the 455-line formatter into `src/services/utils/formatters/`.
- **No hard-coded fallback.** The first registry merged DB values over a hard-coded `_FALLBACK_REGISTRY`, and param maps lived in a hard-coded `ServiceKeys` dict. 66a19c42 (#1683, minimax, 2026-07-23) removed both; `service_keys` moved into the DB (gtwy-node migration `20260723120000-add_service_keys.js`). The docstring now says the DB is the single source of truth. <!-- verify:ignore -->
- **`case _` in `calculate_usage`.** Its comment: without it a new DB-only OpenAI-chat service would extract no usage and bill $0.
- **Cached-token carve-out.** Most providers report cached tokens as a subset of input; billing both would double-charge. Anthropic reports input without cache, so the subtraction is a no-op there; cache-write carve-out is gated on the openai_responses shape for the same reason.
- **Request-model stamping** (#1726): providers return dated snapshots (`gpt-4o` → a dated id); a contextvar avoids threading `model` through ~14 call sites and is inherited by `asyncio.create_task` subtasks.
- **TypeSafe gets its own handler** (#1784, #1786): Jev is not OpenAI compatible and returns 422 for unknown fields, so only model, state and questions are sent. Question types are checked locally because TypeSafe answers anything else with a bare "Invalid request.".
- **OpenAI images inlined**: OpenAI would otherwise download multi-MB files itself and time out (comment in `openai_response.py`).
- **Platform keys swap only when readable**: an empty dict on a transient error would strip every wallet-billed agent of its key (comment in `init_platform_apikeys`).

> [!TODO] Needs a human: the gtwy-node seed migration sets `openai_completion` to `client: "openai_completion_sdk"`, which `uses_openai_sdk` does not match. What does the live `services` document say, and is `openai_completion` (and `minimax`) working in production? <!-- verify:ignore -->

## Changing it safely

There are no automated tests for this area. After any change, call a playground agent on each affected service: non-streaming, streaming, with a tool call, and check `response.usage.cost`.

**Add a model to an existing service (no code).** Insert a `modelconfigurations` document with status 1 (from gtwy-node). The change stream picks it up. Pricing goes in `outputConfig.usage[0].total_cost`; `validationConfig.type` "chat" makes it eligible for the auto router; wallet plans also need it in the plan allowlist ([billing-and-limits](billing-and-limits.md)).

**Add an OpenAI-chat-compatible service** (the provider speaks the Chat Completions API):
1. gtwy-node migration: a `services` doc with `wire_format` openai_chat, `client` openai_sdk, `base_url`, `service_keys.default`, `apikey_status_codes`, flags; plus its `modelconfigurations` docs.
2. Add the key to `service_name` in `src/configs/constant.py` and to `_SERVICE_NAMES` in `src/schemas/completion_schemas.py`.
3. Add it to the list in `BaseService.update_model_response` (`src/services/commonServices/baseService/baseService.py`). If it takes a function-name `tool_choice`, note that the conversion in `service_formatter` is also a hard-coded list.
4. Usage, handler, runner and formatter need nothing: `case _`, `uses_openai_sdk` and `format_openai_compatible` cover it. Example: 66a19c42 "fix: service minimax (#1683)".

**Add a service with its own API** (new wire format or SDK). Follow b13a5c11 "feat: add TypeSafe (Jev) as a service (#1784)":
1. `src/configs/constant.py`: new `WireFormat` / `Client` members and the `service_name` key; `_SERVICE_NAMES` in `src/schemas/completion_schemas.py`.
2. `src/services/commonServices/<name>/`: a Call class (subclass `BaseService`, implement `execute`) and a runner that wraps the call in `execute_api_call`.
3. Branches in `Helper.create_service_handler` (before `uses_openai_sdk` if the name could also match it) and in `BaseService.chats` (and `stream` if it streams).
4. A formatter in `src/services/utils/formatters/` and a branch in `Response_formatter`.
5. A `case` in `TokenCalculator.calculate_usage`.
6. The `services` and `modelconfigurations` documents as a gtwy-node migration.

Batch support is separate: `Helper.create_service_handler_for_batch` and [batch-api](batch-api.md).
