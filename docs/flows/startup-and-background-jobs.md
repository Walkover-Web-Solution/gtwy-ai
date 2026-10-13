---
covers: ["index.py", "globals.py", "src/services/grafana.py", "src/configs/model_configuration.py", "src/configs/service_registry.py", "src/configs/plan_registry.py", "models/Timescale/connections.py", "src/services/utils/auto_router_utils.py", "src/services/utils/batch_script.py", "src/services/utils/built_in_tools/browser/reaper.py", "src/services/commonServices/queueService/baseQueue.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Flow: startup, background jobs and shutdown

What happens between `gunicorn … index:app` and the first request, which loops keep running in the background, and how a pod drains on SIGTERM.

## How the process starts

The Dockerfile runs `opentelemetry-instrument gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080 --timeout 300 index:app`. Every gunicorn worker is a separate process with its own copy of everything below: its own in-memory registries, change-stream listeners, consumer and loops. Locally, `python index.py` runs one uvicorn process on `Config.PORT` (see [environment.md](../environment.md)).

## Steps

1. **Import time.** `index.py` imports `src/services/grafana.py`, which configures Pyroscope profiling (tag `service_type` = consumer when `CONSUMER_STATUS == 'true'`, else producer). It also registers `index.py:_handle_sigterm` for SIGTERM. Importing the routers pulls in `config.py:Config`, the Mongo client (`models/mongo_connection.py`) and the Redis client (`src/services/cache_service.py`). None of these connect until first use.
2. **Registries load before anything else**, in `index.py:lifespan`:
   1. `src/configs/model_configuration.py:init_model_configuration` loads `modelconfigurations` into `model_config_document`, then calls `init_platform_apikeys` (decrypted platform keys for wallet traffic).
   2. `src/configs/service_registry.py:init_service_registry` loads the `services` collection (status 1) into `service_registry_document`.
   3. `src/configs/plan_registry.py:init_plan_registry` loads `billing_plans`.

   See [llm-providers.md](../modules/llm-providers.md) and [billing-and-limits.md](../modules/billing-and-limits.md) for what these registries drive.
3. **RabbitMQ.** `queue_obj` (main queue) and `sub_queue_obj` (log queue) each `connect()` and `create_queue_if_not_exists()`. The metrics queue is not declared here; see [queues-history-metrics.md](../modules/queues-history-metrics.md).
4. **Consumer pods only** (`Config.CONSUMER_STATUS.lower() == "true"`):
   - `consume_messages_in_executor` starts the main-queue consumer (`src/services/commonServices/queueService/queueService.py:Queue.consume_messages`).
   - `src/services/utils/batch_script.py:repeat_function` starts the batch poller.
5. **Timescale.** `models/Timescale/connections.py:init_async_dbservice` runs `SELECT 1`. When `ENVIRONMENT == "LOCAL"` it runs as a background task and does not block startup. A failure is only logged in both cases.
6. **Background tasks start** (none of them block startup):

   | Task | Started from | What it does | Cadence |
   |---|---|---|---|
   | Model config change stream | `src/configs/model_configuration.py:background_listen_for_changes` | Reloads model configs on any `modelconfigurations` change | on change; reconnects after 5–10 s on error |
   | Platform API key change stream | `src/configs/model_configuration.py:background_listen_for_platform_apikey_changes` | Reloads platform keys | on change |
   | Service registry change stream | `src/configs/service_registry.py:background_listen_for_service_changes` | Reloads `services` | on change |
   | Plan change stream | `src/configs/plan_registry.py:background_listen_for_plan_changes` | Reloads billing plans | on change |
   | Auto-router services refresh | `src/services/utils/auto_router_utils.py:run_supported_services_refresh_loop` | Refreshes NotDiamond's supported providers | every 24 h (`SUPPORTED_SERVICES_REFRESH_INTERVAL_SECONDS`; the docstring's "15 days" is wrong) |
   | Browser reaper | `src/services/utils/built_in_tools/browser/reaper.py:run_browser_reaper_loop` | Closes idle Gtwy_Browser tabs | every 60 s; only when Steel is configured, on every pod |
   | Browser cookie indexes | `src/db_services/browserCookieService.py:ensure_indexes` | Creates Mongo indexes once | once; only when Steel is configured |
   | Main-queue consumer | step 4 | Runs queued chat/image requests | continuous; consumer pods only |
   | Batch poller | step 4 | Collects provider batch results ([batch-api.md](../modules/batch-api.md)) | every 900 s; consumer pods only |

7. **Serve.** `/healthcheck` always returns 200. `/ready` returns 200 while `globals.is_ready` is true.

> [!WARNING]
> `CONSUMER_STATUS` must be set on every pod. `index.py:lifespan` calls `.lower()` on it, so an unset value crashes startup. The Pyroscope tag compares it case-sensitively (`'true'`), so `TRUE` starts the consumer but tags the pod as a producer.

> [!WARNING]
> A failed Mongo read during a registry reload empties the model and service registries until the next change event: `init_model_configuration` clears before it updates, and only the platform key and plan registries keep their old data on error. A short Mongo blip can therefore make every model look unknown. Details are in [llm-providers.md](../modules/llm-providers.md).

## Shutdown and draining

1. SIGTERM → `index.py:_handle_sigterm` sets `globals.is_ready = False`.
2. `/ready` now returns 503, so the load balancer stops sending traffic.
3. The consumer keeps its connection, but `BaseQueue._message_handler_wrapper` nacks every new message with `requeue=True` so another pod picks it up.
4. `repeat_function` exits after its current sleep, because its loop condition is `globals.is_ready`.
5. On lifespan exit, every background task is cancelled and both queues disconnect.

Gunicorn's `--timeout 300` bounds how long an in-flight request may run during the drain.

## Where to add a new background job

- Start it in `index.py:lifespan` with `asyncio.create_task(...)`, keep the handle, and cancel it in the shutdown block. Every task started there follows this pattern.
- If it must run on one kind of pod only, put it under the `CONSUMER_STATUS` branch like the consumer and batch poller.
- If it must stop accepting work during a drain, check `globals.is_ready` in its loop.
- If several pods could run it at once, take a Redis lock the way the batch poller does (`acquire_lock` in `src/services/cache_service.py`).
