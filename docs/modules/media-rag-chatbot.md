---
covers: ["src/controllers/image_process_controller.py", "src/controllers/rag_controller.py", "src/routes/image_process_routes.py", "src/routes/rag_routes.py", "src/routes/chatBot_routes.py", "src/schemas/image_schemas.py", "src/schemas/rag_schemas.py", "src/schemas/chatbot_schemas.py", "src/services/utils/gcp_upload_service.py", "src/services/utils/image_compression.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Media, RAG and chatbot endpoints

Three small HTTP surfaces that sit beside the main chat pipeline: file and image upload (to a GCP bucket, or to Gemini for videos), a RAG search endpoint that proxies the external Hippocampus vector service, and the chatbot `sendMessage` endpoint that authenticates a chatbot user and then hands off to the normal chat completion. Two helpers here are shared with the providers: `uploadDoc` (GCP upload) and the image compression used when images are sent to a model. The route table is in [api.md](../api.md); env vars are in [environment.md](../environment.md).

| File | Role |
|---|---|
| `src/routes/image_process_routes.py` | One router with `POST /`, `POST /multi` (no auth) and `POST /upload` (`jwt_middleware`). `index.py:180-181` mounts it twice, at `/image/processing` and at `/files`. |
| `src/controllers/image_process_controller.py` | Upload limits, GCP upload, video upload to the Gemini Files API, PDF URL caching. |
| `src/schemas/image_schemas.py` | `FileUploadRequest` (type checks, PDF/video detection) and `VideoUrlUploadRequest`. |
| `src/services/utils/gcp_upload_service.py` | `uploadDoc`: writes bytes, a file object or a fetched URL to the `resources.gtwy.ai` bucket and returns the public URL. |
| `src/services/utils/image_compression.py` | Downloads images for a model call, downscales big ones, returns base64. Not used by the upload routes. |
| `src/routes/rag_routes.py` | `POST /query` under `/rag`, behind `jwt_middleware`. |
| `src/controllers/rag_controller.py` | `get_vectors_and_text` (the endpoint) and `get_text_from_vectorsQuery` (also used by RAG tools and the RAG pre-tool). |
| `src/schemas/rag_schemas.py` | `RAGQueryRequest`. |
| `src/routes/chatBot_routes.py` | `POST /{botId}/sendMessage` under `/chatbot`, with `combined_auth`. |
| `src/schemas/chatbot_schemas.py` | `ChatbotSendMessageRequest`, a subclass of `CompletionRequest`. |

## File and image upload

### How it works

The same router serves both prefixes, so `/image/processing/upload` and `/files/upload` are the same handler, and so are `/image/processing/multi` and `/files/multi`.

1. **Limits.** `src/controllers/image_process_controller.py:_enforce_upload_limits` runs first on every route. It allows at most `MAX_FILES_PER_REQUEST` (10) files and at most `MAX_TOTAL_UPLOAD_SIZE_BYTES` (35 MB) in total across the request. The total adds `UploadFile.size`, and a missing size counts as 0. Breaking either limit gives a 400.
2. **`POST /` and `POST /multi`** (`image_processing`, `multi_image_processing`). The form field is `image` (repeat it for `/multi`). Each file goes straight to `uploadDoc(folder="uploads", real_time=True)`. There is no type check. `/` raises 400 on failure. `/multi` uploads one file at a time and puts a per-file error in the list instead of failing.
3. **`POST /upload`** (`file_processing`) reads the raw request itself, so FastAPI validates nothing:
   - JSON body → `VideoUrlUploadRequest` → `_process_video_url`.
   - Form with a `video_url` field → the same path.
   - Otherwise it reads every `file` and every `video` form field, applies the limits, and passes each one to `_process_single_file` with the form's `apikey`.
4. **`_process_single_file`** builds `FileUploadRequest`, which rejects any extension in `NON_PREVIEWABLE_EXTENSIONS` (Office files, archives, executables, disk images). It sets `is_pdf` from content type `application/pdf` or a `.pdf` suffix. It sets `is_video` from `VIDEO_CONTENT_TYPES` or `VIDEO_EXTENSIONS`. A video needs a non-empty `apikey`.
   - **Video:** the bytes go to a temp file, then to `genai.Client(api_key=apikey).files.upload`. The handler returns the Gemini file's metadata as `file_data` without waiting for Gemini to finish processing it. The temp file is deleted.
   - **Anything else:** `uploadDoc(folder="uploads", real_time=True)`. The result carries `file_url`.
5. **`_process_video_url`** downloads the URL with aiohttp into a temp file and uploads it to Gemini. It then polls `files.get` every 0.1 s until the state is `ACTIVE`. `FAILED` or more than 300 s ends the wait with an error.
6. **PDF caching.** If PDFs were uploaded and the form has both `thread_id` and `agent_id`, `_cache_pdf_urls` merges their URLs into a list in Redis under `redis_keys['pdf_url_']` + `{agent_id}_{thread_id}_{sub_thread_id or thread_id}`, with a 7-day TTL. The merge replaced an overwrite in #1712, so a second upload adds to the list instead of replacing it. This step is broken today; see the warnings below.

### Where files go

`src/services/utils/gcp_upload_service.py:uploadDoc` builds a storage client on every call from `GCP_CREDENTIALS`, which holds a service-account JSON string. The bucket name `resources.gtwy.ai` and the public URL base are hard-coded. No env var picks the bucket.

| Input | Object name |
|---|---|
| `file` is a `str` (a URL to fetch) | `{folder}/{uuid4}.png` |
| `original_filename` has an extension | `{folder}/{uuid4}.{last extension}` |
| `content_type` contains `image` / `pdf` | `.png` / `.pdf` |
| otherwise | `{folder}/{uuid4}` |

The return value is always `https://resources.gtwy.ai/{object name}`. The original filename is not kept. `real_time=True` uploads before returning. With `real_time=False`, `_upload_background` uploads in an `asyncio.create_task` and the URL comes back at once, even though the object may not exist yet. Only the OpenAI image-URL path uses that mode (`src/services/commonServices/openAI/image_model.py`). User uploads use `folder="uploads"`; the provider image generators (`src/services/commonServices/openAI/image_model.py`, `src/services/commonServices/openAI/openai_response.py`, `src/services/commonServices/Google/gemini_image_utils.py`) use `folder="generated-images"`.

### Image compression (at model-call time, not upload time)

Uploads are stored as sent. Compression happens later, when a completion attaches images. `src/services/utils/image_compression.py:fetch_images_b64` downloads every URL in parallel through `src/services/utils/apiservice.py:fetch`. `compress_image_bytes` runs in `asyncio.to_thread`. It returns `[(base64, mime), ...]`.

- Only `image/jpeg`, `image/jpg`, `image/png` and `image/webp` are re-encoded. Other types pass through.
- An image is left as it is when its longest side is at most `MAX_DIMENSION` (2048 px) and its size is at most `MAX_BYTES` (3 MB).
- Otherwise it is EXIF-rotated, shrunk to fit 2048 px, and saved as WEBP if it has alpha or JPEG if not, both at `QUALITY` 80. If the result is bigger than the original, the original is kept.
- Any Pillow error keeps the original bytes. A failed download still raises; the caller decides what to do.

Callers at this commit: `src/services/commonServices/createConversations.py` (conversation history), `src/services/commonServices/anthropic/anthropicCall.py`, `src/services/commonServices/openaiCompatible/openai_compatible_call.py`, and `src/services/commonServices/openAI/openai_response.py` (through `fetch_images_as_data_urls` → `to_data_urls`). Which providers get base64 and which get plain URLs is in [llm-providers](llm-providers.md).

### Contracts

Request fields for `POST …/upload` (multipart form): `file` (repeatable), `video` (repeatable), `video_url`, `apikey` (Gemini key, required for video), `agent_id`, `thread_id`, `sub_thread_id`. The JSON form is `{"video_url": "https://…/clip.mp4", "apikey": "<gemini key>"}`.

```jsonc
// POST /image/processing/        (form field "image")
{"success": true, "image_url": "https://resources.gtwy.ai/uploads/<uuid>.png"}
// POST /image/processing/multi   — a bare list, not an object
[{"success": true, "image_url": "…"}, {"success": false, "error": "Error in image processing: …"}]

// POST /files/upload, one non-video file   (is_pdf and filename are stripped)
{"success": true, "file_url": "https://resources.gtwy.ai/uploads/<uuid>.pdf"}
// one video file
{"success": true, "file_data": {"name": "files/…", "uri": "…", "mime_type": "video/mp4", "state": "…", "…": "…"},
 "message": "Video uploaded to Gemini successfully"}
// two or more files: success is true only if every file succeeded
{"success": false, "results": [
  {"success": true, "filename": "a.pdf", "file_url": "…"},
  {"success": false, "filename": "b.mp4", "error": "Error in video processing: …"}]}
// video_url (JSON or form)
{"success": true, "file_data": {…}, "message": "Video uploaded and processed successfully by Gemini", "original_url": "…"}
```

Errors raised as `HTTPException` come back as `{"detail": {"success": false, "error": "…"}}`. To use an uploaded video, the client sends `file_data` back as `video_data` on the completion body. `src/services/utils/common_utils.py` reads it, and `src/services/commonServices/Google/gemini_video_model.py` uses its `uri` (or `name`). Gemini files belong to the project of the key that uploaded them, so the completion must use a key from the same project.

### Gotchas

> [!WARNING]
> **PDF caching raises `KeyError` and fails the request.** `redis_keys` in `src/configs/constant.py` has had no `pdf_url_` entry since 6008933f (#1579, 2026-06-01), which renamed every key to a `cd_`/`nd_` prefix. A PDF uploaded to `…/upload` with both `agent_id` and `thread_id` is already in the bucket when `_cache_pdf_urls` raises, and the client gets a 500. With several files, the whole batch returns 500 after every file has been uploaded.

> [!WARNING]
> **Nothing reads this cache anyway.** The reader it was written for is `src/services/utils/common_utils.py:add_files_to_parse_data`, which `src/services/commonServices/common.py` calls when a request has no `files`. That reader still uses `{bridge_id}_{thread_id}_{sub_thread_id}` with no prefix. The upload writer gained the `pdf_url_` prefix in 249f67b6 (#992, 2025-10-16), and gtwy-node's thread-files writer uses `nd_files_`. Neither one matches the reader (see [data-model](../data-model.md)). So PDFs reach the model only when the client puts their URLs in `files` on the completion body. Fix the writer and the reader together.

> [!WARNING]
> **Schema errors on `…/upload` are 500s, not 400s.** `FileUploadRequest.model_validate` and `VideoUrlUploadRequest.model_validate` run inside the handler. They raise a plain pydantic `ValidationError`, and `index.py` only has a handler for `RequestValidationError`. So an unsupported type such as `.docx`, a video without `apikey`, or a `video_url` that does not end in a video extension all give a bare 500. In the multi-file loop, the `except HTTPException` does not catch it, so one bad file fails the whole batch. `VideoUrlUploadRequest` checks the end of the whole URL string, so a signed URL with a query string is rejected.

> [!WARNING]
> **Video URL errors all come back as 400.** `_process_video_url` ends with `except Exception`, which also catches its own `HTTPException`s, the 500 for `FAILED` and the 408 for the timeout, and turns them into a 400. The detail then starts with `Error in video URL processing: 500: …` or `… 408: …`. Status codes inside the `try` mean nothing.

> [!WARNING]
> **These calls block the event loop.** `blob.upload_from_string` / `upload_from_file` (up to 35 MB), the Gemini `files.upload`, and the `files.get` polling (up to 300 s, every 0.1 s) are all synchronous inside `async` handlers. Each one stalls every other request on that worker while it runs. Use `asyncio.to_thread` when you touch them.

> [!WARNING]
> The 35 MB check runs after Starlette has parsed the whole multipart body, so it rejects big uploads but does not stop them from being received. A directly uploaded video returns while Gemini may still be processing it. Only the `video_url` path waits for `ACTIVE`.

> [!TODO] Needs a human: `POST /image/processing/`, `/image/processing/multi`, `/files/` and `/files/multi` have no auth dependency and no type check. Anyone can upload any file, with any client-supplied content type, to the public `resources.gtwy.ai` bucket. Is that intended (for example for embedded chat widgets), or should these routes use `jwt_middleware` like `/upload`?

> [!TODO] Needs a human: why 10 files and 35 MB per request (#1712)? And why are both `/image/processing` and `/files` kept (`/files` was added in 443584bb next to the older prefix)? Which clients still call each one?

## RAG query

`POST /rag/query` (`src/routes/rag_routes.py`, `jwt_middleware`, no rate limit) is a thin proxy to Hippocampus, the external vector store at `hippocampus.gtwy.ai`. This repo has no vector index or embedding code for RAG. Every call sends `x-api-key: HIPPOCAMPUS_API_KEY`. The base URL is hard-coded as plain `http://` in `src/controllers/rag_controller.py`. The response-cache lookup in [chat-orchestration](chat-orchestration.md) uses the same host.

### How it works

1. FastAPI validates the body as `RAGQueryRequest`, so a failure is the standard 422. You must send either `collection_id` and `owner_id`, or `doc_id` / `resource_id`. `top_k` is at least 1 (default 2). `score` is between 0.1 and 1.0 (default 0.1). Extra keys are allowed and ignored.
2. `get_vectors_and_text`:
   - With `collection_id` and `owner_id`, it searches the whole collection. It uses the sentinel resource id `collection_only_query` and maps it to the collection.
   - Otherwise, `resource_id` = `doc_id` or `resource_id`. It sends `GET http://hippocampus.gtwy.ai/resource/{resource_id}` and reads the owner and collection ids from the reply. If the owner id is missing, it returns 400.
3. `get_text_from_vectorsQuery(…, Flag=False)` sends `POST http://hippocampus.gtwy.ai/search` and joins every `result[].payload.content` with newlines.

```jsonc
// RAGQueryRequest
{"query": "refund policy", "top_k": 2, "score": 0.1,
 "doc_id": "<hippocampus resource id>"}            // or "resource_id", or "collection_id" + "owner_id"

// Hippocampus search payload (field names belong to Hippocampus)
{"query": "…", "ownerId": "…", "minScore": 0.1, "top_k": 2,
 "resourceId": "…", "collectionId": "…"}          // collection-only: collectionId, no resourceId

// 200 response from /rag/query
{"success": true, "text": "<chunk 1>\n<chunk 2>", "results": [{"payload": {"content": "…"}, "…": "…"}]}
```

`results` is Hippocampus's `result` array, passed through unchanged. Hippocampus errors are not retried, except for 502/520 and network errors, which `src/services/utils/apiservice.py:fetch` retries 3 times with backoff and a 600 s total timeout.

### Shared with tool calls

`get_text_from_vectorsQuery` also runs RAG for the model:

- RAG tools in [tool-calling](tool-calling.md) (`src/services/commonServices/baseService/utils.py`, tool type `RAG`) call it with `Flag=True`, `owner_id` from the agent, and the tool's `resource_to_collection_mapping`.
- The `rag_knowledgebase` pre-tool in [chat-orchestration](chat-orchestration.md) (`src/services/utils/common_utils.py`) calls it the same way.

With `Flag=True` it returns `{"response": "<merged text>", "metadata": {"type": "RAG"}, "status": 1}`. It never raises: any error becomes `{"response": "<error string>", "status": 0}`. Without a `top_k` in `args` it uses 3, not the endpoint's 2.

### Gotchas

> [!WARNING]
> **A failed search hides its real error.** When `get_text_from_vectorsQuery` fails, its `response` is a string. `get_vectors_and_text` then calls `.get("result")` on that string, raises `AttributeError`, and answers 400 with `{"detail": "'str' object has no attribute 'get'"}`. The `{"success": false}` 400 body in the code can never be returned. Log `text["response"]` before you trust that branch.

> [!TODO] Needs a human: `get_vectors_and_text` never reads `request.state.profile`, so a caller with any valid gtwy JWT can search any `resource_id`, or any `collection_id` + `owner_id`, with the platform's Hippocampus key. Is Hippocampus expected to enforce tenancy, or should the endpoint check that `owner_id` belongs to the caller's org? Also, why is Hippocampus called over plain HTTP with the API key in a header?

## Chatbot sendMessage

`POST /chatbot/{botId}/sendMessage` is the endpoint for embedded chatbots and public agents. It does not have its own pipeline. It authenticates, turns the chatbot body into a normal completion body, and calls the same `chat_completion` handler as `POST /api/v2/model/chat/completion`. Everything after that is in [chat completion flow](../flows/chat-completion.md).

### How it works

1. **Body.** `ChatbotSendMessageRequest` subclasses `CompletionRequest` (#1748), so the parent's field types and `validate_known_service` still apply. It redefines `require_bridge_identifier` (same name, so it replaces the parent's check that `agent_id` is present), because a chatbot names its agent by `slugName`. `require_user_content` needs a non-blank `message` or at least one entry in `images`. A failure is a 422.
2. **Auth** (`src/routes/chatBot_routes.py:combined_auth`). It first runs `chat_bot_auth`: the JWT is checked with `CHATBOTSECRETKEY`, or with `PUBLIC_CHATBOT_TOKEN` when `botId == "Public_Agents"`. Then it rate-limits `body.slugName` to 100 and `body.threadId` to 20 per minute. If any of that raises an `HTTPException`, it tries `agents_auth` (`PUBLIC_CHATBOT_TOKEN`). The claims each path expects, and the bugs in the fallback's rate limit, are in [agent-configuration](agent-configuration.md#auth-paths).
3. **`send_data_middleware`** (`src/middlewares/interfaceMiddlewares.py`):
   - **Public users.** If the profile has an `ispublic` key, a `slugName` of the form `orgId::slugName` sets the org. The agent is looked up with `get_agents_data`: public availability, or private with the user's email in `allowedUsers`. Otherwise it uses `get_bridge_by_slugname(org_id, slugName)`. An unknown slug is a 400.
   - **Blocked orgs** get a 403.
   - **Thread ids.** For a public user the thread id is the token's user id and `threadId` in the body is ignored. `subThreadId` defaults to that id.
   - **Merge.** It writes `request.state.chatbot` (below). Then it calls `add_configuration_data_to_body(schema_class=None)` (the body is already validated) and `chat_completion`.
4. **Reply.** `add_configuration_data_to_body` merges `request.state.chatbot` over the raw JSON body. Keys it does not set, such as `images`, `files` and `configuration`, come from the raw body.
   - `flag: true` → `response_format` `default`: the answer comes back in this HTTP response.
   - `flag: false`, the default → `response_format` RTLayer: `chat_completion` queues the job and returns `{"success": true, "message_id", "message": "Your response will be sent through configured means."}`. The answer is pushed to RTLayer with `RTLAYER_AUTH`.

```python
request.state.chatbot = {
  "bridge_id": str(agent["_id"]),
  "user": message.strip(),
  "thread_id": threadId, "sub_thread_id": subThreadId,
  "variables": {**interfaceContextData, **variables, **json.loads(token "variables")},  # token wins
  "settings": {**configuration.get("settings", {}),
               "response_format": {"type": "default", "cred": {}} if flag
                  else {"type": "RTLayer", "cred": {"channel": channelId, "ttl": 1, "apikey": RTLAYER_AUTH}},
               "max_token": agent.get("max_token") if public else None},
  "chatbot": True, "response_type": {"type": "json_object"},
  "actions": [{"actionId", "description", "type", "variable"}, ...],   # from agent["actions"]
  "bridge_summary": agent.get("bridge_summary"),
}
# channelId = f"{botId}{threadId or userId}{subThreadId or userId}" with spaces replaced by "_"
```

### Gotchas

> [!WARNING]
> `botId` does not choose the agent; `slugName` does. `botId` only names the RTLayer channel and switches the signing key for `Public_Agents`. A client listening on a channel must build the same `channelId` string.

> [!WARNING]
> "Public" means the token has an `ispublic` claim (`"ispublic" in profile`), not that its value is true. A token carrying `ispublic: false` still takes the public path: it is looked up by public availability, gets the user-id thread, and has `max_token` applied.

> [!WARNING]
> Errors other than `HTTPException` inside `send_data_middleware` come back as **400 `{"error": "Error: …"}`**, not as `{"detail": …}`. That includes `get_bridge_by_slugname`'s `BadRequestException("Bridge not found")` and a token without `org`/`user` claims on the fallback path.

## Why it is built this way

- **Videos go to Gemini, not GCP** (6de70613, 6a79134d, Oct 2025). Gemini video understanding reads a Gemini file URI. The key comes from the caller; f8d33337 removed a hard-coded Gemini key and made `apikey` required.
- **Only `video_url` waits for `ACTIVE`** (5d46fc67): this ensures "uploaded files are fully processed before continuing". The direct-upload path never got the same wait. No commit says whether that was on purpose.
- **Several files per request, with per-file results** (16c3f56d, #1712), so one bad file does not fail the batch. With a single file, the old response shape is kept on purpose (comment in `file_processing`).
- **Compression** (#1721, merged in 38aacc53). Large phone photos made OpenAI calls time out. Providers given a URL had to fetch multi-MB files themselves, and base64 adds about a third to the size. The image is now downloaded once, shrunk only if it is over 2048 px or 3 MB, and inlined. The step is best-effort, so it cannot fail a completion. It replaced the uncompressed `fetch_images_b64` in `src/services/utils/apiservice.py`, which nothing imports any more.
- **RAG resource lookup** (6ee75ac4, #1177). Callers often know only a document id, so the endpoint asks Hippocampus for the owner and collection. 883c2c7e added collection-only search, 079970aa added the 0.1–1 `minScore` bound, and 33753af8 (#1051) added the score threshold.
- **Pydantic schemas** for these routes came in 7cb8aaf4 (#1301). The chatbot schema was rebased onto `CompletionRequest` in 7ab86a06 (#1748) so that chatbot bodies get the same field checks as completions.

## Changing it safely

- **Fixing PDF caching:** pick one key and use it in `_cache_pdf_urls`, in `add_files_to_parse_data`, and in gtwy-node's thread-files writer. Today all three differ. The obvious candidate is `redis_keys['files_']`, which gtwy-node already writes; otherwise add a new `nd_` key to `redis_keys` in `src/configs/constant.py` (see [data-model](../data-model.md)). Keep the merge, so a second upload adds to the list instead of replacing it.
- **Upload limits or blocked types:** change the constants at the top of the controller or `NON_PREVIEWABLE_EXTENSIONS`. Both prefixes change together. If you add validation, raise `HTTPException(400, {"success": False, "error": …})`, not `ValueError` in a schema, or the client gets a 500.
- **`uploadDoc`** is also used by the image-generation providers. A change to object naming or the URL base changes the image URLs saved in history. Keep `real_time=True` anywhere the URL is used right away.
- **`get_text_from_vectorsQuery`:** keep the `Flag=True` shape `{response: str, metadata, status}` and the never-raise behaviour. The tool loop and the pre-tool read `status` and `response`.
- **`CompletionRequest`:** a new validator there also applies to chatbot bodies, unless `ChatbotSendMessageRequest` defines a method with the same name.
- There are no automated tests. Check by hand against a local server:

```bash
curl -F "file=@doc.pdf" -F agent_id=<id> -F thread_id=t1 -H "Authorization: <jwt>" localhost:8080/files/upload
curl -F "image=@a.png" -F "image=@b.jpg" localhost:8080/image/processing/multi
curl -H "Authorization: <jwt>" -H "Content-Type: application/json" \
     -d '{"query":"hi","doc_id":"<id>"}' localhost:8080/rag/query
curl -H "Authorization: <chatbot jwt>" -H "Content-Type: application/json" \
     -d '{"slugName":"<slug>","message":"hi","threadId":"t1","flag":true}' localhost:8080/chatbot/<botId>/sendMessage
```
