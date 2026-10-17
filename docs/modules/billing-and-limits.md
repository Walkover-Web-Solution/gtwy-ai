---
covers: ["src/services/billing/**", "src/configs/plan_registry.py", "src/configs/blocked_orgs.py", "src/services/utils/load_plan_configs.py", "src/services/utils/update_and_check_cost.py", "src/services/utils/usage_alert_service.py", "src/services/utils/limit_ttl_utils.py", "src/services/utils/period_key.py", "src/services/utils/api_key_status_helper.py", "src/db_services/apikey_usage_service.py", "src/db_services/api_key_status_service.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Billing and limits

Everything that decides whether a request may run and what it costs: the Lago wallet gate (platform API keys, a flat credit hold on a Redis shadow balance, billing events), billing plans (model allowlists and per-hit fees), spend limits per agent / folder / API key, usage-alert emails, API-key health status and blocked orgs. The middleware calls the gate before every completion; `chat` and its background tasks release the hold and record the cost afterwards.

| File | Role |
|---|---|
| `src/services/billing/billing_utils.py` | The wallet gate `reserve_credits_and_api_key_setup`; holds (`reserve_credits`, `release_credits`, `release_credits_after`); charge builders (`build_llm_usage_event`, `build_hit_fee_event`); shadow debit (`apply_billing_events`); org plan lookup (`get_org_plan`); fallback plan checks (`fallback_allowed_on_plan`, `resolve_wallet_fallback_key`, `apply_wallet_fallback`). Holds the three Lua scripts. |
| `src/services/billing/lago_service.py` | Read-only Lago client: `get_wallet_balance`, `get_subscription_plan_slug`. Own aiohttp session, 3 s total timeout, no retries. |
| `src/configs/plan_registry.py` | In-memory `plan_registry_document` loaded from Mongo `billing_plans`, refreshed by a change stream. Policy functions `plan_allows`, `allowed_models_for`, `hit_fee_for`. |
| `src/services/utils/load_plan_configs.py` | `get_plan_configs`: reads and normalises `billing_plans`. Defines the `ALL_MODELS` sentinel and `HIT_KINDS`. |
| `src/configs/blocked_orgs.py` | `is_org_blocked`: `SISMEMBER` on the blocked-orgs Redis set. |
| `src/services/utils/update_and_check_cost.py` | Spend limits: `check_bridge_api_folder_limits` before the call, `update_cost` and `update_last_used` after it. `purge_related_bridge_caches` and `create_redis_keys` have no callers. |
| `src/services/utils/usage_alert_service.py` | `evaluate_usage_alerts`: threshold, limit-reached and daily-spike emails. |
| `src/services/utils/limit_ttl_utils.py` | `calculate_limit_ttl`: seconds until the next reset, anchored to the limit's start date (agent and folder limits). |
| `src/services/utils/period_key.py` | `period_key` / `redis_ttl_for`: UTC calendar period strings for API-key counters. |
| `src/db_services/apikey_usage_service.py` | Durable API-key spend on `apikeycredentials` (`add_usage`, `get_usage`). |
| `src/services/utils/api_key_status_helper.py` | `mark_apikey_status_from_response`: provider HTTP code to key status, then an RTLayer notice. |
| `src/db_services/api_key_status_service.py` | `update_apikey_status`: writes `status` on `apikeycredentials`. |

Tunables live in `src/configs/constant.py`: `billing_config` (hold size 50 credits, overdraft floor -100), `usage_alert_config`, `usage_mail_types`, `USAGE_ALERT_MAIL_URL`, `limit_types`, `api_key_status`, and the `redis_keys` names. Env vars (`LAGO_API_URL`, `LAGO_API_KEY`, `LAGO_CREDIT_RATE_USD`, `GTWY_COMMISSION_PCT`, `LAGO_PLAN_CODE_FREE`, `LAGO_PLAN_CODE_PAID`, `GTWY_PLATFORM_ORG_ID`) are described in [environment](../environment.md). Every Redis key below gets the `AIMIDDLEWARE_{ENVIRONMENT}_` prefix from `src/services/cache_service.py`; the full key list is in [data model](../data-model.md).

## How it works

### Admission order for one request

1. **Blocked org.** `jwt_middleware` (`src/middlewares/middleware.py`) and the chatbot interface middleware call `is_org_blocked` and answer 403 "Organization is disabled". A Redis error counts as not blocked.
2. **Spend limits.** `getConfiguration` runs `check_bridge_api_folder_limits` for the primary agent and for each connected agent. The primary over its limit returns the limit error as a 400. A connected agent over its limit is dropped from `bridge_configurations` with only a log line ([agent-configuration](agent-configuration.md)).
3. **Wallet gate.** `add_configuration_data_to_body` (`src/middlewares/getDataUsingBridgeId.py`) calls `reserve_credits_and_api_key_setup`. An error dict becomes a 400 `detail`. A hold token is put on the body as `credit_hold_token`.

### The wallet gate: `reserve_credits_and_api_key_setup`

1. Stamps `db_config["billing_attribution"]` with the primary agent's `user_id`, `folder_id` and `is_embed`. The first agent's owner pays for the whole run, nested agents included.
2. For every config in `bridge_configurations`: an agent with its own key gets `wallet=False` (its model usage is free). One without gets `get_platform_apikey(service)` (from `platform_apikey_document`, loaded from Mongo `platform_apikeys`) and `wallet=True`. With no key at all, a non-primary agent is dropped. The primary fails with "Could not find api key or Agent is not Published".
3. If the org is `GTWY_PLATFORM_ORG_ID`, every `wallet` is set False and the gate returns: no plan, no hold. gtwy-node calls our agents as the platform org for background jobs and bills the triggering customer itself.
4. `get_org_plan`. `charge_hit_fee` is true when the plan is in `OWN_KEY_HIT_FEE_PLANS` (`{"paid"}`). If no agent needs the wallet and the plan does not charge own-key hits, the gate returns here and no plan is stamped.
5. Stamps `org_billing_plan` and `charge_hit_fee` on every config. These configs travel into connected-agent, transfer, todo-executor and testcase frames, which is how those frames see the plan. For each wallet config it checks the model exists (and, if private, belongs to this org) in `model_config_document`, then `plan_allows`.
6. Batch, embedding and image requests return without a hold.
7. `reserve_credits`. Refused means error code `CREDIT_BALANCE_EXHAUSTED`, and `_alert_credit_exhausted` fires an `orgCreditExhausted` alert through `unknown_error_handler_alert` as a background task. It fires on every refused request, with no de-dup, and a failed alert is only logged.

### Credit hold lifecycle

The gate reads only the **shadow balance**: a Redis float per org, seeded from Lago, with no TTL. Lago stays the source of truth for money. The keys, after the prefix (the braces are literal: a Redis Cluster hash tag that keeps the balance, its holds and its claims in one slot so one Lua script can touch them all):

```text
nd_billing_credit_balance_{<org_id>}                     shadow balance, float, no TTL
nd_billing_credit_hold_{<org_id>}_<32 hex token>         amount held ("50"), TTL 7200 s
nd_billing_credit_applied_{<org_id>}_<transaction_id>    debit claim, TTL 86400 s
nd_billing_no_wallet_<org_id>                            "Lago has no wallet / is failing", TTL 60 s
nd_org_billing_plan_<org_id>                             plan slug, TTL 86400 s (60 s after a failure)
```

1. **Reserve** (`_RESERVE_CREDITS_SCRIPT`, atomic). If the balance key is missing, return MISSING. If `balance - 50 < -100`, return REJECT. Otherwise `INCRBYFLOAT -50` and write a single-use hold token key holding `"50"` with a 7200 s TTL.
2. On MISSING or REJECT, `_sync_balance_from_lago` reads the summed balance of all active Lago wallets, writes it with `SET NX`, and the reserve runs once more.
3. **Carry.** The token rides on the request body. `chat` pops `credit_hold_token`, so the frame that pops it owns the release.
4. **Charge.** After a successful call, `process_background_tasks` publishes the frame's billing events on the log queue and then calls `apply_billing_events`, which takes the real cost off the shadow balance (see [Charging](#charging)).
5. **Release** (`_RELEASE_CREDITS_SCRIPT`). It gives back exactly the amount stored in the token, then deletes the token. A second release, or a stale token, is a no-op. If the balance key has vanished, nothing is credited, because a fresh Lago seed already excludes the hold.

Who releases, by path:

| Path | Release happens in |
|---|---|
| Middleware fails after the reserve | `add_configuration_data_to_body.__call__` (both `except` branches) |
| Queue publish fails | `chat_completion` in `src/routes/v2/modelRouter.py` |
| Failure in `chat_multiple_agents` before `chat` ran | its `except` (release is token-idempotent) |
| Non-streaming chat | `chat`'s `finally`, after the background tasks have charged |
| Streaming (new SSE, or an agent transfer on an existing SSE) | `release_credits_after(task, ...)`, which waits for the `sse_stream_and_finalize` task |
| Queued request (non-default `response_format`) | the queue consumer runs `chat`, which pops the token |
| Rerun | `rerun_messages_route` `finally`: one release for N queued copies. Copies carry no token (`build_rerun_queue_message`) |
| Testcases | reserved in `src/services/testcase_service.py:get_testcase_configuration`, popped and released in `run_for_version_model` |

The hold is an admission check, not a budget: a call that costs more than 50 credits still runs, and the real cost is charged afterwards.

### When Lago is down or the wallet is empty

| Situation | Result |
|---|---|
| Redis error during reserve | Admitted with no hold ("fail open"). Charging still happens, gated by `wallet`. |
| No balance key yet | Seeded from Lago with `SET NX`, reserve retried. |
| Org has no active Lago wallet, or Lago errors or takes over 3 s | `nd_billing_no_wallet_{org_id}` is set for 60 s and the request is admitted with no hold. Later requests in those 60 s skip Lago. |
| Org has several active wallets | Balances are summed and an error is logged (org 20678 once drained a second wallet to -284 while the gate read the first). |
| Balance below the floor | REJECT, re-sync (cannot change an existing key), REJECT again → 400 `CREDIT_BALANCE_EXHAUSTED`. |
| Plan lookup fails (Lago unreachable) | Plan `DEFAULT_PLAN_CODE` ("free"), cached 60 s. |
| `billing_plans` never loaded | `plan_allows` allows everything (logged once a minute); `hit_fee_for` charges nothing. |
| `LAGO_API_URL` set but `LAGO_CREDIT_RATE_USD` missing or ≤ 0 | The process refuses to start (`_load_credit_rate` raises at import). |

### Charging

`compute_billing_events` (`src/services/commonServices/baseService/utils.py`, owned by [tool-calling](tool-calling.md)) builds a frame's events once and caches them in `parsed_data["_billing"]`:

- a usage event when the frame ran on the wallet (`wallet`), plus one for `_wallet_primary_cost` when a wallet primary burned tokens and then fell back: to the customer's own key (`apply_wallet_fallback`), or after a response cut off by the token limit (`finish_reason` "truncated", in `chat`). If the truncation fallback fails, `chat` returns the primary answer and drops `_wallet_primary_cost`, because the primary is then billed through the normal usage event;
- a hit-fee event when `wallet`, `_wallet_primary_cost` or `charge_hit_fee` is set. `hit_type` is `embed` (attribution `is_embed`), `chatbot` (`bridgeType`) or `api`.

`process_background_tasks` publishes them under the `billing` key of the log-queue message, merged with `model`, `service`, `bridge_id`, `thread_id` and the payer fields. gtwy-node posts them to Lago. The error path emits no events, so a failed request is never charged.

```text
base_credits = round_half_up(cost_usd / LAGO_CREDIT_RATE_USD, 4 dp)
credits      = round_half_up(base_credits * (1 + GTWY_COMMISSION_PCT/100), 4 dp)   # usage event
fee_credits  = round_half_up(plan.hit_fees[hit_type] / LAGO_CREDIT_RATE_USD, 4 dp) # hit fee, never commissioned
```

```jsonc
// build_llm_usage_event (example at a rate of 0.0025 and 10% commission)
{ "type": "llm_usage_debit", "transaction_id": "llm-usage-<message_id>-<bridge_id>-<8 hex nonce>",
  "message_id": "…", "org_id": "…", "credits": "12.3640",
  "cost_usd": "0.0281", "base_credits": "11.2400", "commission_pct": "10" }
// build_hit_fee_event
{ "type": "hit_fee", "transaction_id": "hit-fee-<message_id>", "message_id": "…", "org_id": "…",
  "credits": "28.0000", "fee_usd": "0.07", "hit_type": "api", "plan": "paid" }
```

Shadow debit: `apply_debit` runs `_DEBIT_SCRIPT`, which claims the applied key for this `transaction_id` (`SET NX EX 86400`) and then decrements. gtwy-node runs the same claim after its Lago post, so whichever runs first debits and the other sees DUPLICATE. The hit fee's id has no nonce and no bridge id, so every frame of one request emits the same id and the dedup layers keep one fee per request. `cost_usd` stays the real provider cost because metrics and spend alerts read it.

### Billing plans

A `billing_plans` document (status 1) is an allowlist of the services and models wallet credits may be spent on, plus per-hit fees:

```jsonc
{ "plan_code": "free", "display_name": "Free", "status": 1,
  "services": { "neev_cloud": "*", "open_router": ["a:free", "b:free"] },   // or "services": "*"
  "hit_fees": { "api": 0.07, "chatbot": 0.07, "embed": 0.06 } }
```

`get_plan_configs` normalises it to `{plan_code, display_name, all_services, services, hit_fees}`. `"*"` becomes `ALL_MODELS`, model lists become frozensets, and fees become Decimals (a missing, zero, negative or non-numeric fee is dropped, so that kind is not charged). If one plan allows nothing, or the load fails, the whole load returns `{}` and `init_plan_registry` keeps the last good registry. On every change, the stream listener reloads and posts `billing_plans_updated` to the RTLayer channel `global_model_updates`.

Which plan an org is on: `get_org_plan` reads the org's plan key (listed above). On a miss it asks Lago for the active subscription and maps its `plan_code` to a slug through `LAGO_PLAN_CODE_FREE` / `LAGO_PLAN_CODE_PAID`. The slug must also exist in the registry, otherwise the org gets `DEFAULT_PLAN_CODE`. A good answer is cached for 1 day (gtwy-node deletes the key on a plan change); a failure is cached for 60 s.

The plan is enforced wherever a request can change which model runs: the per-agent gate, both fallback paths (`fallback_allowed_on_plan` skips the fallback, `apply_wallet_fallback` raises), the auto router (`allowed_models_for` in `src/services/auto_router_service.py`) and the todo executor's fallback (`resolve_wallet_fallback_key`). Agents on the customer's own key are never restricted.

### Spend limits (agent, folder, API key)

Limits are in **provider USD** (`tokens.total_cost`), not credits: the commission and hit fees do not count. A limit of 0 or less is off.

**Check** (`check_bridge_api_folder_limits`, before the call): folder, then agent, then API key. Usage at or above the limit returns:

```jsonc
{ "success": false, "error": "Bridge limit exceeded. Used: 5.2/5", "error_code": "BRIDGE_LIMIT_EXCEEDED",   // FOLDER_… / APIKEY_…
  "limit_type": "bridge", "current_usage": "5.2", "limit_value": "5" }
```

**Keys and reset:**

```text
nd_bridgeusedcost_<bridge_id>    JSON {usage_value, versions, reset_period, setup_date}            TTL = calculate_limit_ttl
nd_folderusedcost_<folder_id>    JSON {usage_value, versions, bridges, reset_period, setup_date}   TTL = calculate_limit_ttl
nd_apikeyperiodcost_<apikey_id>_<period>   bare float, INCRBYFLOAT          TTL 2 / 15 / 65 days, set once (EXPIRE NX)
nd_apikeyperiodrefs_<apikey_id>_<period>   set of "v:<version_id>", "b:<bridge_id>"  (gtwy-node uses it to purge agent caches)
apikeycredentials.{apikey_usage, apikey_usage_period}   durable API-key copy (Mongo)
```

- **Agent and folder:** the key expiring *is* the reset. `calculate_limit_ttl` counts to the next reset anchored on `*_limit_start_date`: daily at the anchor's time of day, weekly on its weekday, monthly on its day of month (clamped to short months). With no anchor it is midnight, Monday or the 1st. An unknown period is treated as daily. For versions, `parent_id` is the counter id, so all versions share one counter. On a Redis miss the counter is seeded from the agent's `bridge_usage` / folder's `folder_usage` field.
- **API key:** the window is in the key name (`period_key`: `2026-08-25`, ISO week `2026-W35`, `2026-08`, all UTC), so a new window reads as zero with no reset job. `apikey_limit_start_date` is ignored. An unknown period is treated as monthly. On a Redis miss, `get_usage` reads the Mongo copy only if `apikey_usage_period` matches, then reseeds Redis.

**Write** (`update_cost`, after a successful completion). `chat`'s `finally` calls `update_cost_usage_and_apikey_status_in_background`, which starts `update_cost_and_last_used` as a background task. Agent and folder do a read, add and write on the JSON blob and keep the key's remaining TTL. The API key does an atomic `INCRBYFLOAT`, plus `add_usage`, a pipeline update that resets and adds in one step. `update_last_used` writes `nd_bridgelastused_` / `nd_apikeylastused_` (gtwy-node's cron copies them to the Mongo last_used field).

### Usage alerts

`update_cost` then calls `evaluate_usage_alerts`. For each scope with a limit configured:

1. Adds the cost to the UTC day bucket `nd_dailyusedcost_` + `<scope>_<id>_<YYYYMMDD>` (TTL 8 days).
2. **Spike:** needs at least 3 non-empty days among the previous 7. If today is over 3× their average, it sends `USAGE_DAILY_SPIKE`, once per day.
3. **Limit / threshold:** usage at or over the limit sends `USAGE_LIMIT_REACHED`. Otherwise usage at 80% or more sends `USAGE_THRESHOLD_REACHED`.

Each email is claimed once through `acquire_lock` (SET NX). The resulting keys are `lock_nd_usagealertsent_<scope>_<id>_threshold` or `..._limit` (TTL until the limit resets) and `lock_nd_usagespikealert_<scope>_<id>_<YYYYMMDD>` (2 days). The email goes out as a POST to `USAGE_ALERT_MAIL_URL` with `mail_type`, `scope`, agent, org and folder ids, usage figures and `environment`. Separately, `update_cost` POSTs every cost update for an agent with any limit to a hard-coded viaSocket flow (`_notify_cost_update`). All of this is self-guarded and never fails the request.

### API key status

`mark_apikey_status_from_response` runs as a background task after each call, for the original service (unless the key came in the request, `is_request_apikey`) and for the fallback service. `get_api_key_status` maps the HTTP code through the service's `apikey_status_codes` (from the DB service registry; [llm-providers](llm-providers.md)) to an `api_key_status` value. 5xx maps to `service_down`, no code to `working`, and "limited" is stored as `exhausted`. If the status changed, it updates `apikeycredentials.status` and sends `apikey_status_update` to RTLayer channel `org_{org_id}`.

## Gotchas

> [!WARNING]
> **Never `SET` the shadow balance.** Seed it with `SET NX` and change it only with `INCRBYFLOAT` / the Lua scripts. Overwriting erases the decrement of every in-flight hold, and their later releases then credit money that was never spent.

> [!WARNING]
> **A hold token is released exactly once, by whoever popped it.** Pop `credit_hold_token` off the body before building child bodies; testcases and reruns do this so a copy cannot release the parent's hold. A streaming path must hand the token to `release_credits_after`. Releasing in `chat`'s own `finally` would return the hold while the model is still streaming.

> [!WARNING]
> **Compute billing events once per frame.** `build_llm_usage_event` mints a random nonce into `transaction_id`. Calling it twice for one call publishes two different charges. Always go through `compute_billing_events` (cached in `_billing`).

> [!WARNING]
> **The hit fee id must stay `hit-fee-{message_id}`.** Adding a bridge id, `hit_type`, plan or nonce charges the fee once per agent frame instead of once per request.

> [!WARNING]
> **Only the gate's loop over `bridge_configurations` checks the plan.** Anything that adds an agent config in the middle of a request, or switches service/model (a new fallback path, a new router), bypasses the plan unless it calls `plan_allows` / `resolve_wallet_fallback_key` itself.

> [!WARNING]
> **Never rebind `plan_registry_document`.** Importers hold the reference. Use `.clear()` + `.update()`. Plan service keys and model names are compared case-sensitively against `cfg["service"]` and `configuration.model`.

> [!WARNING]
> **Spend limits overshoot.** The check runs before the call and the cost is added afterwards in a background task, so concurrent requests can all pass. Agent and folder counters are a read, add and write on a JSON blob, so increments that finish together are lost (the API-key counter was moved to `INCRBYFLOAT` for this reason).

> [!WARNING]
> **Two defaults for an unknown reset period:** `calculate_limit_ttl` treats it as daily, `period_key` as monthly.

> [!WARNING]
> **Alert de-dup keys start with `lock_`, not `nd_`** (`acquire_lock` adds the prefix). A Redis scan for `*_nd_usagealertsent_*` finds nothing.

> [!WARNING]
> **A plan document added after an org's plan was cached does not apply right away.** If Lago reports a slug that is not yet in `billing_plans`, `get_org_plan` caches `"free"` for a full day. Delete that org's `nd_org_billing_plan_` key after adding the plan.

> [!TODO] Needs a human: a REJECT also calls `_sync_balance_from_lago`. Its `SET NX` cannot change an existing balance, so every refused request costs one Lago GET. If that GET fails, the org is admitted and, through the 60 s no-wallet marker, stays admitted for 60 s even though its balance is below the floor (`reserve_credits`). Is fail-open intended for exhausted orgs?

> [!TODO] Needs a human: API-key alert keys use `redis_ttl_for` (65 days for monthly, 15 for weekly, 2 for daily), but the key name has no period (`_check_threshold_and_limit`). A threshold email sent in August therefore blocks the September one too. Should the period go into the key name?

> [!TODO] Needs a human: the gate skips the hold for image, embedding and batch, and its docstring says they are "not billed per-event yet". But `src/services/commonServices/common.py:image` reaches `process_background_tasks`, so wallet image calls are charged with no balance check. `embedding` and `batch` run on the platform key with `wallet=True` and emit no events. Which is intended?

> [!TODO] Needs a human: `_HOLD_TOKEN_TTL` says a leaked hold is surfaced by "the reconciliation job". Neither repo has an automatic shadow-balance reconcile (gtwy-node only offers an admin refresh). The balance key has no TTL, so a hold whose token expired (for example a queued message that waited over 2 h) stays deducted for good. Who reconciles?

> [!TODO] Needs a human: plan-mode primary-agent sub-tasks run with `skip_history=True` (`src/services/todo/executor_service.py`), and `process_background_tasks` returns before `compute_billing_events`. Is their wallet usage charged anywhere ([todo-mode](todo-mode.md))?

## Cross-repo contracts

> [!WARNING]
> gtwy-node (the sibling Node repo, AI-middleware) writes or reads the same money and keys. Change these in both repos in the same release, or charges and gates disagree with no error.

<!-- verify:ignore-start -->
| Contract | This repo | gtwy-node |
|---|---|---|
| `GTWY_COMMISSION_PCT` identical, same rounding order (round base to 4 dp half-up, then multiply) | `billing_utils.py` `_load_commission_pct` (logged at startup) | `src/services/logQueue/backgroundJobBilling.service.js` |
| `LAGO_CREDIT_RATE_USD` identical | `_load_credit_rate` | same file; `createWallet` |
| `LAGO_PLAN_CODE_FREE` / `LAGO_PLAN_CODE_PAID` map to slugs `free` / `paid`; slugs equal `billing_plans.plan_code` | `lago_service.py` `_PLAN_CODE_TO_SLUG` | `src/configs/billingPlans.js` |
| Same Lago instance (env names differ) | `LAGO_API_URL`, `LAGO_API_KEY` | `BILLING_API_URL`, `BILLING_API_KEY` |
| Key names byte-identical, same `AIMIDDLEWARE_{ENVIRONMENT}_` prefix, `{org_id}` hash tag on balance and applied keys | `redis_keys` in `src/configs/constant.py` | `redis_keys` in `src/configs/constant.js` |
| Applied-claim TTL 86400 and the same `_DEBIT_SCRIPT` logic | `_APPLIED_TTL` | `APPLIED_TTL`, `DISPATCHED_TTL` in `billingDebit.service.js` |
| Shadow balance: webhook credits use `incrementRedisBalance` (INCRBYFLOAT). The admin top-up and admin refresh use `syncWalletBalanceToRedis`, a plain SET that drops the decrements of in-flight holds | seeds with `SET NX` | `src/services/lago.service.js` |
| Deletes the org's `nd_org_billing_plan_` key on a plan change (Python caches it for a day) | `get_org_plan` | `lago.service.js` |
| Writes the blocked-orgs set (SADD/SREM) | reads in `is_org_blocked` | `src/controllers/blockedOrg.controller.js` |
| Period strings identical, UTC, ISO weeks | `period_key` | `src/services/utils/periodKey.utils.js` |
| Consumes the `billing` list (needs `org_id`, `credits`, `transaction_id`; refuses the platform org) and `background_billing` | `process_background_tasks` | `src/consumers/logQueueConsumer.js` → `processBillingEvents` |
| `GTWY_PLATFORM_ORG_ID` identical | gate skip | debit refusal |
| Seeds `billing_plans` (`scripts/bootstrapBilling.js`, edited via `PUT /api/billing-plans`) and `platform_apikeys` (`scripts/seedPlatformApiKeys.js`) | reads only | owner |
| Lago metric `gtwy_hit_fee` with a charge on every plan; without it Lago accepts hit fees and never bills them | emits `hit_fee` | `walletDebit`, docs/lago-runbook.md |
<!-- verify:ignore-end -->

## Why it is built this way

- **Shadow balance plus a flat hold, not a Lago call per request.** Lago is on the request path only when the cache misses. `get_wallet_balance` and `get_subscription_plan_slug` use their own 3 s, no-retry session, because the shared `fetch` allows 600 s plus retries and would freeze user requests on a slow Lago.
- **Fail open on billing infrastructure, fail closed on plans.** Redis or Lago trouble admits the request with no hold, so billing can never take the platform down; charging is gated by `wallet`, not by the hold. Plan resolution and the allowlist fail closed (an unknown org gets the most restrictive plan). The one fail-open case is a registry that never loaded, because denying would return 400 on every wallet request.
- **Single-use hold tokens.** They replaced earlier double-release bugs: redeliveries and overlapping cleanup paths used to give back the same hold twice (38aacc53, "charge wallet credits" #1718).
- **Lago is the only record of an org's plan** (no Mongo copy): one place to be wrong instead of two.
- **Hit fee rate from the plan, not env.** `GTWY_HIT_FEE_USD` was one platform-wide number that needed a restart to change. <!-- verify:ignore --> Per-plan `hit_fees` lets one plan get a different rate with an admin call. `hit_fee_for` is deliberately not fail-open: not knowing a price must charge nothing.
- **`OWN_KEY_HIT_FEE_PLANS`.** Orgs on `paid` (shown as Pro) pay the per-hit fee even on their own provider key, and an empty wallet blocks them too. Free-plan own-key traffic stays free.
- **Period-keyed API-key counters** (#1723). This fixed three bugs. The usage was never durable (the Redis to Mongo sync was commented out). The read, add and write lost concurrent increments (300 parallel $0.01 adds kept $0.01). And the reset depended on exact TTL arithmetic on every write. Agent and folder limits have not migrated yet, so both mechanisms run side by side.
- **Credit rate checked at startup.** A missing rate used to make every usage event quietly return None, which silently turned billing off.

## Changing it safely

- **New plan or plan change:** edit `billing_plans` through gtwy-node; no deploy is needed here. A new plan needs its Lago plan code mapped in both repos (`LAGO_PLAN_CODE_*` only knows free and paid) and a `gtwy_hit_fee` charge in Lago. <!-- verify:ignore -->
- **New code path that runs an agent or swaps the model:** carry `wallet`, `org_billing_plan` and `charge_hit_fee` from the config, call `plan_allows` before spending wallet credits, and either own a hold token (pop it, release it once) or carry none.
- **Changing the commission, credit rate, key names, the debit script or period formats:** change gtwy-node in the same release (table above).
- **Moving agent and folder limits to period keys:** follow the API-key pattern (`_write_apikey_usage`, `_apikey_usage_for_period`, and `_current_usage` in the alert service), and update the gtwy-node readers of `nd_bridgeusedcost_` / `nd_folderusedcost_`.
- There are no automated tests in either repo for this area (the `period_key` docstring mentions shared test vectors that do not exist). Check by hand on a test org: watch the `[billing]` log lines and compare the shadow balance with the Lago wallet. See also [chat-orchestration](chat-orchestration.md) and the [chat-completion flow](../flows/chat-completion.md).
