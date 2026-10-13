---
covers: ["index.py", "src/routes/**", "src/schemas/**", "src/middlewares/**"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# API

One FastAPI app, `index.py:app`. Every route is listed here; behaviour behind a route lives in the module docs it links to.

## How a full path is built

No router sets its own prefix (all are a bare `APIRouter()`), so the full path is the mount prefix plus the route path.

| Mount prefix | Line | Router |
|---|---|---|
| (app) | `index.py:151-166` | `/healthcheck`, `/ready` |
| `/api/v2/model` | `index.py:177` | `src/routes/v2/modelRouter.py:router` |
| `/browser` | `index.py:178` | `src/routes/v2/browserRouter.py:router` |
| `/chatbot` | `index.py:179` | `src/routes/chatBot_routes.py:router` |
| `/image/processing` | `index.py:180` | `src/routes/image_process_routes.py:router` |
| `/files` | `index.py:181` | the same `image_process_routes` router, mounted a second time |
| `/rag` | `index.py:182` | `src/routes/rag_routes.py:router` |

> [!WARNING]
> The image router's first route is `POST /`, so its real paths end in a slash: `/files/` and `/image/processing/`. A POST to `/files` gets a 307 redirect from Starlette, and many HTTP clients drop the body or switch to GET when they follow it.

## Auth

Routes declare auth as FastAPI dependencies (`dependencies=[Depends(...)]`), not as app middleware. The detailed rules (profile shape, proxy lookups, rate-limit keys) are in [agent-configuration](modules/agent-configuration.md#auth-paths).

| Scheme | What the caller sends | Checked by | Routes |
|---|---|---|---|
| User JWT | `Authorization: <jwt>` (HS256, `SecretKey`), the raw token with no `Bearer ` prefix | `src/middlewares/middleware.py:jwt_middleware` | model routes except `openai/responses`, `/rag/query`, `/upload` |
| Embed token | `Authorization: <jwt>` whose payload has a top-level `folder_id`, HS256-signed with the org's own `meta.gtwyAccessToken` (not `SecretKey`) | `src/middlewares/middleware.py:jwt_middleware` → `src/services/proxy/embed_service.py:resolve_gtwy_embed_token` (finds or creates the MSG91 user) | same as the user JWT. Missing claims or no org secret → 401; bad signature → 404 |
| MSG91 proxy | `proxy_auth_token` header, or `pauthkey` header | `src/middlewares/middleware.py:make_data_if_proxy_token_given` (inside `jwt_middleware`) | same as the user JWT |
| OpenAI SDK | `Authorization: Bearer <pauthkey>` | `src/middlewares/openai_sdk_middleware.py:openai_sdk_middleware` rewrites it into a `pauthkey` header, then runs `jwt_middleware` | `openai/responses` |
| Chatbot JWT | `Authorization: [Bearer ]<jwt>` signed with `CHATBOTSECRETKEY`, or `public_chatbot_token` for `botId == "Public_Agents"` | `src/middlewares/interfaceMiddlewares.py:chat_bot_auth`, falling back to `src/middlewares/agentsMiddlewares.py:agents_auth` | `/chatbot/{botId}/sendMessage` |
| Live-link token | the JWT in the path, signed with `SecretKey` | `src/routes/v2/browserRouter.py:live_view` | `/browser/live/{token}` |
| None | — | — | `/healthcheck`, `/ready`, `POST /files/`, `/files/multi` (and the `/image/processing` twins) |

Auth status codes from `jwt_middleware`:

| Case | Status |
|---|---|
| No `Authorization`, `proxy_auth_token` or `pauthkey` header | **404** `"unauthorized user"` (not 401) |
| JWT does not decode, or a proxy lookup throws | 401 `"unauthorized user"` |
| Token in the blacklist (`is_token_blacklisted`) | 401 `"token revoked"` |
| Bad `pauthkey` | 401 `"invalid pauthkey"` |
| Org in the blocked set (`is_org_blocked`) | 403 `"Organization is disabled"` |
| Over the rate limit (`src/middlewares/ratelimitMiddleware.py:rate_limit`) | 429 with `Retry-After` |

> [!WARNING]
> `jwt_middleware` passes the whole `Authorization` value to `jwt.decode`. `Bearer <jwt>` fails with 401. Only `chat_bot_auth` strips a `Bearer ` prefix.

## Error shapes

A client can get four different error bodies. Check which one a route produces before parsing.

| Source | Status | Body |
|---|---|---|
| `HTTPException` from a dependency or route | as raised | `{"detail": "<string>"}` or `{"detail": {"success": false, "error": "…"}}` |
| Schema validation (`RequestValidationError`) | 422 | `{"detail": "Validation error", "errors": [ …pydantic errors… ]}` from `index.py:validation_exception_handler` |
| Failure inside `chat` / `batch` / `embedding` / `image` | 400 | the error dict at the top level, no `detail` (`src/handler/executionHandler.py:handle_exceptions`; see [chat-orchestration](modules/chat-orchestration.md#contracts)) |
| Any other exception | 500 | a plain-text Python traceback |

> [!WARNING]
> `index.py` builds the app with `FastAPI(debug=True)`. Any exception that is not an `HTTPException` or a `RequestValidationError` returns its full traceback (file paths, code lines) to the caller. A Pydantic `ValidationError` raised by a plain `model_validate` call inside a handler is one of these: it is a 500 traceback, not a 422.

Schemas are validated in two ways. A route parameter typed with a model (`/chatbot`, `/rag`) is validated by FastAPI. The model routes call `model_validate` inside `add_configuration_data_to_body` or `direct_auth_and_rate_limit` and re-raise as `RequestValidationError`, so they also get the 422 shape.

CORS allows every origin, method and header (`CORSMiddleware` in `index.py`).

## App routes

| Method | Path | Auth | Handler | Response |
|---|---|---|---|---|
| GET | `/healthcheck` | none | `index.py:healthcheck` | 200 `{"status": "OK running good... v1.3"}`. Checks nothing. |
| GET | `/ready` | none | `index.py:ready` | 200 `{"status": "ready"}`; 503 `{"status": "shutting down"}` after SIGTERM |

`_handle_sigterm` in `index.py` sets `globals.is_ready = False`. That flips `/ready` to 503 so the load balancer drains the pod, makes queue consumers requeue new messages (`src/services/commonServices/queueService/baseQueue.py`), and stops the batch cron and the browser reaper loops. Use `/ready`, not `/healthcheck`, as the readiness probe.

## Model router

Mounted at `/api/v2/model` in `index.py:177`. Handlers are in `src/routes/v2/modelRouter.py`.

| Method | Path | Auth dependency | Handler | Request | Notes |
|---|---|---|---|---|---|
| POST | `/api/v2/model/chat/completion` | `auth_and_rate_limit` | `chat_completion` | `CompletionRequest` | JSON, SSE, or queued |
| POST | `/api/v2/model/ai/completion` | `direct_auth_and_rate_limit` | `ai_completion` | `DirectCompletionRequest` | no agent id; runs on a hidden per-org agent |
| POST | `/api/v2/model/openai/responses` | `openai_sdk_middleware` | `openai_sdk_responses` | OpenAI Responses body (no model class) | sync only |
| POST | `/api/v2/model/batch/chat/completion` | `auth_and_rate_limit` | `batch_chat_completion` | `BatchChatCompletionRequest` | provider batch API |
| POST | `/api/v2/model/testcases` | `auth_and_rate_limit` | `run_testcases_route` | none (raw dict) | returns at once; results on RTLayer |
| POST | `/api/v2/model/rerun` | `auth_and_rate_limit` | `rerun_messages_route` | none (`schema_class=None`) | queues reruns |

Every route except `/testcases` also depends on `add_configuration_data_to_body` (`src/middlewares/getDataUsingBridgeId.py`). It validates the schema, loads the agent, reserves a credit hold, and answers 400 with `{"detail": {...}}` for: an agent that cannot be loaded, a credit or plan refusal ([billing-and-limits](modules/billing-and-limits.md)), no resolvable configuration, `"User message is compulsory"`, and `"model or service does not exist!"`. Details: [agent-configuration](modules/agent-configuration.md#building-the-configuration-add_configuration_data_to_body).

### `POST /api/v2/model/chat/completion`

Request: `src/schemas/completion_schemas.py:CompletionRequest`. `extra="allow"`, so undeclared keys (for example `mode`, `action` for [todo-mode](modules/todo-mode.md#how-it-is-switched-on)) pass through to the pipeline.

```json
{
  "agent_id": "…",              // or "bridge_id" (alias), or "version_id": one of the three is required
  "user": "hello",              // required unless images, image/audio user_urls, batch or a rerun
  "thread_id": "…", "sub_thread_id": "…",
  "variables": {},
  "service": "openai",          // optional override; must be in _SERVICE_NAMES
  "configuration": {            // optional overrides on the agent config; extra keys allowed
    "model": "…", "type": "chat",            // chat | embedding | image | reasoning | fine-tune
    "stream": false,
    "response_format": {"type": "webhook", "cred": {"url": "https://…", "headers": {}}}
  },
  "user_urls": [{"type": "image", "url": "https://…"}],
  "stream": true,               // wins over configuration.stream
  "is_playground": false
}
```

Validation (422 when broken):
- `require_bridge_identifier`: one of `agent_id`, `bridge_id`, `version_id`.
- `service` must be in the `_SERVICE_NAMES` literal and in `service_name` (`src/configs/constant.py`). The two lists are kept in sync by hand.
- `configuration.model`, when sent, is a strict non-empty string.
- `response_format` with `type: "webhook"` needs `cred.url` matching `HTTP_URL_REGEX` (`src/schemas/_validators.py`). This applies to `configuration.response_format`; a top-level `settings.response_format` is an undeclared extra and is not validated.
- `user_urls[].url` must be http or https.

What comes back depends on the final `response_format` (request merged over the agent) and streaming:

| Case | Response |
|---|---|
| `type` `default` or missing, stream off | 200 JSON, shape in [chat-orchestration](modules/chat-orchestration.md#contracts) |
| `type` `default`, stream on | 200 `text/event-stream`, events in [chat-orchestration](modules/chat-orchestration.md#streaming-sse) |
| any other `type` (`webhook`, `RTLayer`) | published to the RabbitMQ main queue; 200 `{"success": true, "message_id": "…", "message": "Your response will be sent through configured means."}` |
| `is_playground: true` and the agent's stream setting is off, `"default"` or unset | `response_format` is forced to RTLayer on channel `{org}_{bridge}[_{version}]_{user}`, then queued as above |
| `is_playground: true` with `type: "webhook"` | runs synchronously instead of queueing |
| queue publish fails | credit hold released, 500 `{"detail": "Failed to publish message."}` |

`configuration.type` picks the pipeline: `embedding` → `embedding`, `image` → `image`, anything else → `chat_multiple_agents` (all in `src/services/commonServices/common.py`).

### `POST /api/v2/model/ai/completion`

A completion without a user-created agent. `direct_auth_and_rate_limit`:
1. `jwt_middleware`, then rate limit 100/60 s per `profile.org.id`.
2. Validates `DirectCompletionRequest`: `service` and `configuration.model` are required; `agent_id`, `bridge_id` and `version_id` are rejected (422, "use /chat/completion").
3. `fill_model_defaults`, then `get_or_create_direct_agent` writes the hidden agent's id into the cached body as `bridge_id`, and sets `request.state.is_direct_call`.
4. Rate limit 20/60 s per `body.thread_id`.

Then it runs `chat_completion` unchanged. Because the agent is empty, the request's own `settings` become the agent settings. See [agent-configuration](modules/agent-configuration.md#the-direct-agent-post-apiv2modelaicompletion).

### `POST /api/v2/model/openai/responses`

An OpenAI-Responses-compatible front for an existing agent, so the OpenAI SDK can call gtwy with `base_url` pointed here. `src/services/utils/openai_sdk_utils.py:build_and_override_request_body` rewrites the body before auth runs:

```json
{"agent_id": "…", "input": "text, or a list of messages (all text is joined into one user turn)",
 "model": "optional, becomes configuration.model", "conversation_id": "… (or thread_id)", "sub_thread_id": "…",
 "variables": {}, "configuration": {}, "service": "…", "apikey": "…", "user_urls": [], "auto_model_select": {}, "cache_on": false}
```

Response (`format_openai_response`):

```json
{"id": "resp_…", "object": "response", "created_at": 1760000000, "status": "completed", "model": "…",
 "output": [{"id": "rs_…", "type": "reasoning", "summary": []},
            {"id": "msg_…", "type": "message", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "…"}]}],
 "usage": {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
           "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 0},
 "user": null, "output_text": "…", "finish_reason": "stop"}
```

Errors: 401 without `Authorization: Bearer …`; 400 `"No user message found in payload."`; an upstream `success: false` keeps its `status_code` (default 400). Streaming and queued replies cannot be formatted ([chat-orchestration gotchas](modules/chat-orchestration.md#gotchas)).

> [!WARNING]
> A body without `agent_id` hits `None.strip()` in `_extract_agent_identifier`. That runs in the dependency before any try/except, so the caller gets a 500 traceback, and gets it even without valid auth.

### `POST /api/v2/model/batch/chat/completion`

Request: `src/schemas/batch_schemas.py:BatchChatCompletionRequest`, a `CompletionRequest` plus:
- `batch`: list of strings, at least one (required).
- `webhook`: `{"url": "https://…", "headers": {}}` (required; `url` must match `HTTP_URL_REGEX`).
- `batch_variables`: optional list of dicts, same length as `batch`.

Response 200 `{"success": true, "response": "<provider message>", "batch_id": "…", "messages": [...]}` (`batch_id` and `messages` only when the provider returns them). Failures are 400 through `handle_exceptions`. Results reach the webhook later through the batch cron ([batch-api](modules/batch-api.md)).

### `POST /api/v2/model/testcases`

No schema; `src/services/testcase_service.py:validate_testcase_request_data` reads the dict.

```json
{"bridge_id": "… (required here)", "version_ids": ["…"],
 "testcase_id": "…", "testcase_ids": ["…"], "testcases": true, "testcase_data": {…},
 "variables": {}, "matching_type": "…", "model": "…", "service": "…", "models": [{"model": "…", "service": "…"}]}
```

Missing `bridge_id` is a 400. Everything else is checked inside the background task: the route returns 200 at once with `{"success": true, "channel_id": "{org_id}_{bridge_id}_{user_id}", "message": "…"}`, and validation errors arrive as a `run_failed` event on that RTLayer channel, not as an HTTP error. Events: `run_started`, `testcase_result`, `version_completed`, `run_completed`, `run_failed`. This route does not run `add_configuration_data_to_body`. See [testcases](modules/testcases.md).

### `POST /api/v2/model/rerun`

Reruns stored messages with the agent's current configuration. No schema validation. Body is either `{"bridge_id": "…", "message_ids": ["…"]}` or `{"bridge_id": "…", "thread_id": "…", "sub_thread_id": "…"}` (reruns the last message of the thread).

| Case | Response |
|---|---|
| queued | 200 `{"success": true, "message": "N message(s) queued for rerun.", "queued": [...], "not_found": [...]?, "history_count": n?}` |
| neither form given | 400 |
| nothing found | 404 |
| anything else | 500 `{"detail": {"success": false, "error": "Internal server error: …"}}` |

The middleware's credit hold covers only this request: `rerun_messages_route` pops `credit_hold_token` from the body before queueing and calls `release_credits` in `finally`, whatever happened.

## Browser router

Mounted at `/browser` in `index.py:178`.

| Method | Path | Auth | Handler | Response |
|---|---|---|---|---|
| GET | `/browser/live/{token}` | the token itself | `src/routes/v2/browserRouter.py:live_view` | 302 to the Steel live view of the conversation's current tab; small HTML pages for 400 (invalid token), 404 (no open tab), 410 (expired), 503 (no debug URL) |

The token is an HS256 JWT signed with `SecretKey`, claims `o` (org), `t` (thread), `s` (sub-thread) and `exp`, minted by `src/services/utils/built_in_tools/browser/steel_client.py:permanent_live_url`. Anyone holding the link can watch the tab until it expires. See [gtwy-browser](modules/gtwy-browser.md#contracts).

## Chatbot router

Mounted at `/chatbot` in `index.py:179`.

| Method | Path | Auth | Handler | Request |
|---|---|---|---|---|
| POST | `/chatbot/{botId}/sendMessage` | `src/routes/chatBot_routes.py:combined_auth` | `src/routes/chatBot_routes.py:send_message` → `send_data_middleware` → `chat_completion` | `ChatbotSendMessageRequest` |

`combined_auth` tries `chat_bot_auth` plus rate limits (100/60 s per `body.slugName`, 20 per `body.threadId`). If that raises any `HTTPException`, including a 429, it tries `agents_auth`; if that fails too, it raises the first error. A missing token is a 498.

Request (`src/schemas/chatbot_schemas.py:ChatbotSendMessageRequest`):

```json
{"slugName": "my-agent",          // required; "orgId::slugName" when the token is public
 "message": "hi",                 // message or images is required
 "images": [], "threadId": "…", "subThreadId": "…",
 "flag": false,                   // false: answer goes to RTLayer; true: answer in the HTTP response
 "variables": {}, "interfaceContextData": {}, "configuration": {"settings": {}}}
```

It subclasses `CompletionRequest` with the agent-id check turned off, so the other `CompletionRequest` fields (`service`, `user_urls`, …) are still validated. The agent is found by slug: `get_bridge_by_slugname` for private tokens, `get_agents_data` for public ones (`src/db_services/ConfigurationServices.py`).

| Case | Response |
|---|---|
| `flag: false` | queued; 200 ack as for queued chat completions. The answer is pushed to RTLayer channel `{botId}{threadId or userId}{subThreadId or userId}` with spaces replaced by `_`. |
| `flag: true` | the synchronous chat result |
| slug not found | 400 `{"detail": "Invalid bridge Id"}` |
| org blocked | 403 |
| any other exception | 400 `{"error": "Error: …"}` (no `detail`) |

See [media-rag-chatbot](modules/media-rag-chatbot.md).

## File and image router

The same router is mounted at `/image/processing` (`index.py:180`) and `/files` (`index.py:181`); each route below exists under both prefixes. Handlers are in `src/routes/image_process_routes.py`, logic in `src/controllers/image_process_controller.py`.

| Method | Path | Auth | Handler | Request | Response |
|---|---|---|---|---|---|
| POST | `/files/` | **none** | `image` → `image_processing` | multipart field `image` (one file) | `{"success": true, "image_url": "…"}` |
| POST | `/files/multi` | **none** | `multi_image` → `multi_image_processing` | multipart field `image`, repeated | list of `{"success": true, "image_url"}` or `{"success": false, "error"}` |
| POST | `/files/upload` | `jwt_middleware` | `upload` → `file_processing` | multipart or JSON, below | below |

All three enforce `MAX_FILES_PER_REQUEST` (10) and `MAX_TOTAL_UPLOAD_SIZE_BYTES` (35 MB combined) with a 400. Non-video files go to the GCS bucket `resources.gtwy.ai` through `src/services/utils/gcp_upload_service.py:uploadDoc`.

> [!WARNING]
> `POST /files/` and `POST /files/multi` (and their `/image/processing` twins) have no auth dependency. Anyone who can reach the service can write files to the public bucket.

`/upload` accepts:
- JSON `{"video_url": "https://…/clip.mp4", "apikey": "<Gemini key>"}` (`VideoUrlUploadRequest`): downloads the video, uploads it to Gemini Files and polls until `ACTIVE` (up to 300 s, 408 on timeout). Returns `{"success": true, "file_data": {…}, "message": "…", "original_url": "…"}`.
- multipart: `file` and/or `video` (repeatable), `video_url`, `apikey` (required for video), `agent_id`, `thread_id`, `sub_thread_id`. Each file is checked by `FileUploadRequest`: extensions in `NON_PREVIEWABLE_EXTENSIONS` (Office, archives, binaries) are refused. Videos go to Gemini, everything else to GCS.
  - one file: `{"success": true, "file_url": "…"}` (videos: `file_data` and `message` instead of `file_url`)
  - several: `{"success": <all succeeded>, "results": [{"success", "filename", "file_url" | "file_data" | "error"}]}`

> [!WARNING]
> `FileUploadRequest` and `VideoUrlUploadRequest` are checked with plain `model_validate`, so a refused file type or a missing `apikey` comes back as a 500 traceback, not a 422. In the multi-file loop only `HTTPException` is caught per file, so one refused file fails the whole request.

> [!WARNING]
> Uploading a PDF with both `agent_id` and `thread_id` calls `_cache_pdf_urls`, which reads `redis_keys['pdf_url_']`. That key was removed from `src/configs/constant.py` in 6008933f, so the call raises `KeyError` and the request returns a 500 after the file is already in GCS. Nothing reads that cache.

## RAG router

Mounted at `/rag` in `index.py:182`.

| Method | Path | Auth | Handler | Request |
|---|---|---|---|---|
| POST | `/rag/query` | `jwt_middleware` (no rate limit) | `src/routes/rag_routes.py:get_query` → `src/controllers/rag_controller.py:get_vectors_and_text` | `RAGQueryRequest` |

`src/schemas/rag_schemas.py:RAGQueryRequest`: `query` (required), `top_k` (≥ 1, default 2), `score` (0.1 to 1.0, default 0.1), and either `collection_id` + `owner_id` or `doc_id` / `resource_id` (422 otherwise). The controller calls Hippocampus at the hard-coded `http://hippocampus.gtwy.ai` with `HIPPOCAMPUS_API_KEY`.

Response: 200 `{"success": true, "text": "<all chunks joined>", "results": [...]}`. Every failure is 400 `{"detail": "…"}`. A failed Hippocampus search included: `get_text_from_vectorsQuery` then returns a string as `response`, the `.get` on it raises, and the detail is that Python error message rather than the search error.
