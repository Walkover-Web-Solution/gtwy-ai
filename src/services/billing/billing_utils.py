import secrets
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from config import Config
from globals import logger
from models.mongo_connection import db

from src.configs.constant import billing_config
from src.configs.plan_registry import DEFAULT_PLAN_CODE, plan_allows, plan_display_name, plan_exists
from src.services.billing.lago_service import get_wallet_balance
from src.services.cache_service import REDIS_PREFIX, client

_CREDIT_QUANTUM = Decimal("0.0001")


def _load_credit_rate() -> Decimal | None:
    """Read LAGO_CREDIT_RATE_USD once, at import time, and fail LOUDLY.

    A missing/zero rate used to silently disable all billing (every
    build_llm_usage_event swallowed the error and returned None). Now: if Lago
    billing is configured (LAGO_API_URL set) the process refuses to start
    without a valid positive rate.
    """
    raw = Config.LAGO_CREDIT_RATE_USD
    try:
        rate = Decimal(str(raw)) if raw is not None else None
    except (InvalidOperation, ValueError):
        rate = None
    if rate is not None and rate > 0:
        return rate
    if Config.LAGO_API_URL:
        raise RuntimeError(
            f"LAGO_CREDIT_RATE_USD is missing or invalid ({raw!r}) while LAGO_API_URL is set — "
            "billing would silently charge nothing. Set a positive rate to start."
        )
    return None


_CREDIT_RATE = _load_credit_rate()


def build_llm_usage_event(usage: dict, message_id: str, org_id: str, bridge_id: str | None = None) -> dict | None:
    try:
        raw = (usage or {}).get("expectedCost")
        cost_usd = Decimal(str(raw or 0))
        if cost_usd <= 0:
            return None
        if _CREDIT_RATE is None:
            logger.error(
                f"[billing] dropping usage event for org {org_id}: no credit rate configured "
                f"(cost_usd={cost_usd})"
            )
            return None
        credits = (cost_usd / _CREDIT_RATE).quantize(_CREDIT_QUANTUM, rounding=ROUND_HALF_UP)
        # bridge_id keeps ids unique across agent-to-agent frames (which share the
        # request-level message_id); the random suffix keeps them unique when the
        # SAME agent runs more than once in one request (tool loops, A→B→A
        # transfers). The event dict is built exactly once per frame and the same
        # dict travels through the queue, so redeliveries carry the same id and
        # still dedup correctly.
        nonce = secrets.token_hex(4)
        transaction_id = (
            f"llm-usage-{message_id}-{bridge_id}-{nonce}" if bridge_id else f"llm-usage-{message_id}-{nonce}"
        )
    except Exception as e:
        logger.error(f"[billing] failed to build usage event for org {org_id}: {e}")
        return None

    return {
        "type": "llm_usage_debit",
        "transaction_id": transaction_id,
        "message_id": message_id,
        "org_id": org_id,
        "credits": str(credits),
        "cost_usd": str(cost_usd),
    }


_BALANCE_KEY = "nd_billing_credit_balance_"
_APPLIED_KEY = "nd_billing_credit_applied_"
_HOLD_KEY = "nd_billing_credit_hold_"
_NO_WALLET_KEY = "nd_billing_no_wallet_"
# Matches Node's DISPATCHED_TTL (86400) — a shorter TTL here let redeliveries
# between hour 2 and 24 re-debit the shadow balance while Node correctly
# dropped the Lago post, drifting the two apart.
_APPLIED_TTL = 86400
# How long an unreleased hold token lives. Long enough for the slowest real
# request (streaming + tool loops); after that a crashed process leaks at most
# one flat hold, which the reconciliation job surfaces.
_HOLD_TOKEN_TTL = 7200
# How long "this org has no Lago wallet" is remembered, so an unprovisioned org
# fails open WITHOUT hammering Lago on every request.
_NO_WALLET_TTL = 60

# Balance check runs BEFORE the claim so a failed write never strands the
# claim: MISSING is returned without claiming, the caller syncs and retries,
# and the retry claims + debits atomically in one script.
_DEBIT_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 0 then
  return 'MISSING'
end
local claimed = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2])
if not claimed then
  return 'DUPLICATE'
end
redis.call('INCRBYFLOAT', KEYS[2], -tonumber(ARGV[1]))
return 'OK'
"""

# Reserve = decrement the balance AND record a one-time hold token holding the
# exact amount, in one atomic step. Release consumes the token, so a hold can
# be released at most once no matter how many code paths try.
_RESERVE_CREDITS_SCRIPT = """
local key = KEYS[1]
local token_key = KEYS[2]
local hold = tonumber(ARGV[1])
local floor = tonumber(ARGV[2])
local token_ttl = tonumber(ARGV[3])

if redis.call('EXISTS', key) == 0 then
  return {'MISSING', '0'}
end

local balance = tonumber(redis.call('GET', key))
if balance == nil then
  return {'MISSING', '0'}
end

local projected = balance - hold
if projected < floor then
  return {'REJECT', tostring(balance)}
end

redis.call('INCRBYFLOAT', key, -hold)
redis.call('SET', token_key, ARGV[1], 'EX', token_ttl)
return {'ADMIT', tostring(projected)}
"""

# Give back exactly the amount the token recorded, once. A second release of
# the same token is a no-op, and a release whose balance key vanished (fresh
# resync from Lago already excludes local holds) must NOT credit on top.
_RELEASE_CREDITS_SCRIPT = """
local amount = redis.call('GET', KEYS[1])
if not amount then
  return 'NOOP'
end
redis.call('DEL', KEYS[1])
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('INCRBYFLOAT', KEYS[2], tonumber(amount))
  return 'RELEASED'
end
return 'NO_BALANCE'
"""

_debit_script = client.register_script(_DEBIT_SCRIPT)
_reserve_credits_script = client.register_script(_RESERVE_CREDITS_SCRIPT)
_release_credits_script = client.register_script(_RELEASE_CREDITS_SCRIPT)


def _key(org_id: str) -> str:
    return f"{REDIS_PREFIX}{_BALANCE_KEY}{{{org_id}}}"


def _hold_key(org_id: str, token: str) -> str:
    # Hash tag {org_id} keeps the token in the same cluster slot as the balance
    # so the Lua scripts can touch both.
    return f"{REDIS_PREFIX}{_HOLD_KEY}{{{org_id}}}_{token}"


def _no_wallet_key(org_id: str) -> str:
    return f"{REDIS_PREFIX}{_NO_WALLET_KEY}{org_id}"


# --- Org billing plan -----------------------------------------------------
# Which plan an org is on. Written by Node (org_billing collection + the Redis
# key) on provision and plan change; read here on the hot path. WHAT a plan
# includes lives in the billing_plans collection (src/configs/plan_registry.py),
# not here — this only resolves the code.
#
# Fail-closed: an org we cannot classify, or one pointing at a plan that does
# not exist, is treated as DEFAULT_PLAN_CODE (the most restrictive plan we
# ship). Worst case a paid org briefly sees "upgrade", never the reverse.
#
# The plan code is validated against the LIVE registry rather than a hardcoded
# tuple. The old ("free","paid") literals here silently coerced any other value
# to "free" — so adding a plan in Node without shipping Python first would have
# restricted every org on it.
_ORG_PLAN_KEY = "nd_org_billing_plan_"
_ORG_PLAN_CACHE_TTL = 3600
_org_billing_collection = db["org_billing"]


def _org_plan_key(org_id: str) -> str:
    return f"{REDIS_PREFIX}{_ORG_PLAN_KEY}{org_id}"


def _resolve_plan_code(raw, org_id: str) -> str:
    """Accept a plan code only if the registry actually defines it."""
    if plan_exists(raw):
        return raw
    if raw:
        logger.error(
            f"[billing] org {org_id} is on unknown plan '{raw}' — no such document in "
            f"billing_plans. Falling back to '{DEFAULT_PLAN_CODE}'; insert the plan "
            "document before pointing an org at it."
        )
    return DEFAULT_PLAN_CODE


async def get_org_plan(org_id: str) -> str:
    """Return the org's plan code, or DEFAULT_PLAN_CODE when it cannot be resolved."""
    try:
        cached = await client.get(_org_plan_key(org_id))
        if cached is not None:
            return _resolve_plan_code(_decode(cached), org_id)
    except Exception as e:
        logger.error(f"[billing] org plan cache read failed for org {org_id}: {e}")
    plan = DEFAULT_PLAN_CODE
    try:
        doc = await _org_billing_collection.find_one({"org_id": str(org_id)}, {"plan": 1})
        plan = _resolve_plan_code(doc.get("plan") if doc else None, org_id)
        await client.set(_org_plan_key(org_id), plan, ex=_ORG_PLAN_CACHE_TTL)
    except Exception as e:
        logger.error(f"[billing] org plan lookup failed for org {org_id}: {e}")
    return plan


def get_platform_apikey(service: str | None) -> str | None:
    """The platform's own provider key for a service (wallet-billed traffic).

    SOLE source is the platform_apikeys Mongo collection (decrypted into
    platform_apikey_document at startup, refreshed by its own change stream).
    The collection MUST be seeded (Node: scripts/seedPlatformApiKeys.js)
    before wallet traffic can run — there is no env fallback.
    """
    try:
        # Call-time import: model_configuration imports baseService.utils, which
        # imports this module — a top-level import closes that loop at startup.
        from src.configs.model_configuration import platform_apikey_document

        return platform_apikey_document.get(service)
    except Exception:
        return None


def fallback_allowed_on_plan(parsed_data: dict) -> bool:
    """A wallet-paid run must not fall back onto a model outside its plan.

    Fallbacks with their own (customer) apikey are never restricted — they do
    not spend wallet credits, so they short-circuit before the plan is read.
    That short-circuit is also what makes an ABSENT org_billing_plan safe:
    reserve_credits_and_api_key_setup stamps the plan only when the request
    actually needed the wallet, so absent implies wallet=False everywhere and
    we already returned above. Do not "helpfully" default the plan here.

    Fail-closed on the allowlist. The single fail-open condition (the registry
    never loaded) is decided once, inside plan_allows.
    """
    fallback = (parsed_data.get("settings") or {}).get("fall_back") or {}
    if not parsed_data.get("wallet") or fallback.get("apikey"):
        return True
    plan = parsed_data.get("org_billing_plan")
    # `or`, not a two-arg .get: getConfiguration_utils writes the fall_back keys
    # unconditionally, so service/model can be present-but-None and .get(k, d)
    # would hand None straight to the predicate.
    service = fallback.get("service") or parsed_data.get("service")
    model = fallback.get("model") or parsed_data.get("model")
    if plan_allows(plan, service, model):
        return True
    logger.warning(
        f"[billing] skipping fallback to '{model}' ({service}) — not included in plan "
        f"'{plan}' for org {parsed_data.get('org_id')}"
    )
    return False


def resolve_wallet_fallback_key(parsed_data: dict, fallback_service, fallback_model):
    """For a fallback that STAYS on the wallet, return (platform_apikey, skip_reason).

    A fallback is a service+model switch that never passes back through the
    per-agent gate in reserve_credits_and_api_key_setup, so both checks happen
    here. Two independent reasons to refuse, both of which used to be soft:

      * the target is outside the org's plan;
      * we have no platform key for the target service — the old code logged a
        warning and ran anyway with the ORIGINAL service's credential, which at
        best fails auth and at worst reaches the wrong provider.
    """
    plan = parsed_data.get("org_billing_plan")
    if not plan_allows(plan, fallback_service, fallback_model):
        return None, (
            f"'{fallback_model}' ({fallback_service}) is not included in plan "
            f"'{plan_display_name(plan)}'"
        )
    key = get_platform_apikey(fallback_service)
    if not key:
        return None, f"no platform api key for fallback service '{fallback_service}'"
    return key, None


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _sync_balance_from_lago(org_id: str) -> bool:
    """Seed the shadow balance from Lago. Returns True when a balance is in place.

    Seeds with NX only: overwriting a live balance would erase the decrements of
    every in-flight hold (phantom credits). Orgs without a wallet are
    negative-cached for _NO_WALLET_TTL so they fail open without hitting Lago
    on every request.
    """
    try:
        if await client.get(_no_wallet_key(org_id)) is not None:
            return False
        balance = await get_wallet_balance(org_id)
    except Exception as e:
        logger.error(f"[billing] could not sync wallet balance for org {org_id}: {e}")
        try:
            await client.set(_no_wallet_key(org_id), "1", ex=_NO_WALLET_TTL)
        except Exception:
            pass
        return False
    await client.set(_key(org_id), str(balance), nx=True)
    return True


async def reserve_credits(org_id: str) -> tuple[bool, str | None]:
    """Try to place a flat hold on the org's shadow balance.

    Returns (admitted, token):
      (True, "<token>")  hold placed — caller owes exactly one release with the token
      (True, None)       admitted WITHOUT a hold (fail open: Redis/Lago trouble)
      (False, None)      rejected — balance exhausted
    """
    key = _key(org_id)
    token = secrets.token_hex(16)
    keys = [key, _hold_key(org_id, token)]
    args = [
        str(billing_config["reserve_credits_per_request"]),
        str(billing_config["reserve_overdraft_floor"]),
        str(_HOLD_TOKEN_TTL),
    ]

    try:
        status = _decode((await _reserve_credits_script(keys=keys, args=args))[0])
        if status in ("MISSING", "REJECT"):
            if not await _sync_balance_from_lago(org_id):
                # No wallet / Lago unreachable → fail open, no hold.
                return True, None
            status = _decode((await _reserve_credits_script(keys=keys, args=args))[0])
    except Exception as e:
        logger.error(f"[billing] reserve failed open for org {org_id}: {e}")
        return True, None

    if status == "REJECT":
        return False, None
    if status == "ADMIT":
        return True, token
    # Balance still missing after a successful-looking sync — fail open.
    return True, None


async def apply_debit(org_id: str, credits, transaction_id: str | None = None) -> None:
    """Decrement the shadow balance by a call's real cost.

    Idempotent on transaction_id, mirroring Lago's own dedup: a redelivered queue
    message resolves to the same id and is a no-op here, so one real charge can
    never decrement this counter twice.
    """
    amount = credits if isinstance(credits, Decimal) else Decimal(str(credits))
    if amount <= 0:
        return
    key = _key(org_id)
    try:
        if transaction_id:
            claim_key = f"{REDIS_PREFIX}{_APPLIED_KEY}{{{org_id}}}_{transaction_id}"
            status = _decode(await _debit_script(keys=[claim_key, key], args=[str(amount), _APPLIED_TTL]))
            if status == "DUPLICATE":
                return
            if status == "MISSING":
                if not await _sync_balance_from_lago(org_id):
                    logger.error(
                        f"[billing] shadow debit skipped for org {org_id} tx {transaction_id}: no balance to debit"
                    )
                    return
                status = _decode(await _debit_script(keys=[claim_key, key], args=[str(amount), _APPLIED_TTL]))
                if status == "MISSING":
                    logger.error(
                        f"[billing] shadow debit lost for org {org_id} tx {transaction_id}: balance vanished mid-sync"
                    )
        else:
            if await client.get(key) is None and not await _sync_balance_from_lago(org_id):
                return
            await client.incrbyfloat(key, float(-amount))
    except Exception as e:
        logger.error(f"[billing] shadow debit failed for org {org_id} tx {transaction_id}: {e}")


async def apply_billing_events(events: dict | list[dict] | None) -> None:
    """Mirror the published billing events into the Redis gate balance."""
    for event in ([events] if isinstance(events, dict) else events or []):
        try:
            await apply_debit(event["org_id"], event["credits"], event.get("transaction_id"))
        except Exception as e:
            logger.error(f"[billing] shadow debit failed for event {event.get('transaction_id')}: {e}")


async def reserve_credits_and_api_key_setup(
    org_id: str, db_config: dict, is_batch: bool = False
) -> tuple[str | None, dict | None]:
    """Fill in the platform apikey and hold credits for it, in one place.

    setup_api_key leaves apikey as None when a bridge has no key of its own.
    Each bridge decides for ITSELF: with its own key it runs free (wallet=False);
    without one it gets the platform key and wallet=True, so only its usage is
    debited. One hold covers the request whenever any bridge runs on wallet.

    Only chat-type requests get a hold. Embedding/image/batch are not billed
    per-event yet and nothing on those paths releases a hold, so one placed for
    them could only leak — they still get keys, wallet flags and attribution.
    The request type is read off the primary config here; `is_batch` has to be
    passed because the batch payload lives on the request body, not db_config.

    Returns (hold_token, error). A non-None token means a hold was actually
    placed — the caller owes exactly one release_credits(org_id, token).
    Billing is gated per-agent by the wallet flag, not by the hold (fail-open
    still bills).
    """
    configs = db_config.get("bridge_configurations") or {}
    primary = configs.get(db_config.get("primary_bridge_id")) or next(iter(configs.values()), {})

    # Who this run is billed to. The FIRST agent's owner pays for the whole
    # run, including nested/connected agents owned by someone else — the
    # attribution travels with the request into child frames and is stamped on
    # every billing event. Recorded here (usage data can never be backfilled).
    db_config["billing_attribution"] = {
        "user_id": primary.get("user_id"),
        "folder_id": primary.get("folder_id"),
        "is_embed": bool(primary.get("is_embed")),
    }

    wallet_needed = False
    dead_bridges = []
    for bridge_key, cfg in configs.items():
        if cfg.get("apikey"):
            cfg["wallet"] = False
            continue
        cfg["apikey"] = get_platform_apikey(cfg.get("service"))
        cfg["wallet"] = bool(cfg["apikey"])
        if not cfg["apikey"]:
            # No own key and no platform key for that service → the agent cannot
            # run. Drop it now with a clear log instead of letting it reach the
            # provider SDK with apikey=None (opaque crash mid-request).
            logger.warning(
                f"[billing] no platform api key for service '{cfg.get('service')}' — "
                f"dropping bridge {bridge_key} from request (org {org_id})"
            )
            if cfg is not primary:
                dead_bridges.append(bridge_key)
        wallet_needed = wallet_needed or cfg["wallet"]

    for bridge_key in dead_bridges:
        del configs[bridge_key]

    if not primary.get("apikey"):
        return None, {
            "success": False,
            "error": "Could not find api key or Agent is not Published",
        }

    # Internal platform traffic: Node's background jobs (suggestions, gpt memory,
    # canonicalizer, thread titles) call our OWN agents over the public API with
    # GTWY_PAUTH_KEY, so they arrive authenticated as the platform org. Those runs
    # must never be wallet-billed or gated here — the cost is charged to the
    # triggering customer in Node instead. Without this, two of those agents
    # (gpt_memory, canonicalizer) have no key of their own, so they take the
    # platform key, flip wallet=True, and bill the platform org: today Lago drops
    # the events because that org has no wallet, but the moment it is provisioned
    # the platform pays for every customer's background jobs AND an empty platform
    # wallet blocks them for everyone. Keyed off the authenticated org, never off a
    # request field, so a customer cannot forge it.
    if Config.GTWY_PLATFORM_ORG_ID and str(org_id) == str(Config.GTWY_PLATFORM_ORG_ID):
        for cfg in configs.values():
            cfg["wallet"] = False
        return None, None

    if not wallet_needed:
        return None, None

    # A plan is an ALLOWLIST of services, optionally narrowed to specific models
    # per service (src/configs/plan_registry.py, editable via the billing_plans
    # collection). Checked PER AGENT, so a disallowed model cannot hide behind an
    # allowed front agent. Agents on the customer's OWN key (wallet=False) are
    # never restricted.
    #
    # This is the ONLY pre-execution gate and the only loop over every entry in
    # bridge_configurations, which is exactly what makes nested / connected /
    # transfer agents covered: a transfer can only target an agent already in
    # this map. Anything that ever introduces an agent config MID-request would
    # bypass the plan entirely.
    plan = await get_org_plan(org_id)
    db_config["org_billing_plan"] = plan
    for cfg in configs.values():
        # Stamp the plan on EVERY agent config, wallet or not. bridge_configurations
        # is the object that travels into connected-agent, transfer, todo-executor
        # and testcase frames — each rebuilds its request body from scratch but
        # merges its primary cfg into it — so this one assignment is what makes the
        # plan visible in those frames. Invariant that follows, and that the
        # fail-closed predicates below depend on:
        #
        #   org_billing_plan is present on a cfg IFF the request needed the wallet.
        #   Absent implies wallet=False everywhere, and every plan predicate
        #   short-circuits on `wallet` before reading the plan.
        cfg["org_billing_plan"] = plan
        if not cfg.get("wallet"):
            continue
        cfg_service = cfg.get("service")
        cfg_model = (cfg.get("configuration") or {}).get("model")

        # Existence BEFORE plan, so a typo'd model never reports as a plan
        # problem and the plan error can't double as an existence oracle for
        # another org's private models. Same deliberately opaque message the
        # middleware uses (getDataUsingBridgeId.py) — and unlike that copy, this
        # covers every agent, not just the primary. Runs before reserve_credits,
        # so no hold is placed for a request that was never going to run.
        from src.configs.model_configuration import model_config_document  # lazy: import cycle

        entry = (model_config_document.get(cfg_service) or {}).get(cfg_model)
        if not entry or (entry.get("org_id") and str(entry["org_id"]) != str(org_id)):
            return None, {"success": False, "error": "model or service does not exist!"}

        if not plan_allows(plan, cfg_service, cfg_model):
            plan_name = plan_display_name(plan)
            return None, {
                "success": False,
                "error": (
                    f"Model '{cfg_model}' ({cfg_service}) isn't included in your {plan_name} "
                    "plan. Add your own API key to use this model, or upgrade your plan to "
                    "unlock it."
                ),
                # Kept verbatim: the frontend (not in either repo) branches on it.
                # Node never reads error_code, so the new fields are additive.
                "error_code": "FREE_PLAN_MODEL_RESTRICTED",
                "plan_error_code": "PLAN_MODEL_NOT_ALLOWED",
                "plan": plan,
                "plan_name": plan_name,
                "service": cfg_service,
                "model": cfg_model,
            }

    request_type = (primary.get("configuration") or {}).get("type")
    if is_batch or request_type in ("embedding", "image"):
        return None, None

    admitted, token = await reserve_credits(org_id)
    if not admitted:
        return None, {
            "success": False,
            "error": "Insufficient credits. Please top up your wallet to continue. For support contact support@gtwy.ai",
            "error_code": "CREDIT_BALANCE_EXHAUSTED",
        }

    return token, None


async def release_credits(org_id: str, token: str | None) -> None:
    """Give a hold back, exactly once.

    The token is single-use: the Lua script credits back the amount recorded at
    reserve time only if the token still exists, then deletes it. Calling this
    twice (queue redelivery, overlapping cleanup paths) or with a foreign/stale
    token is harmless — that was the root of the double-release bugs.
    """
    if not token:
        return
    try:
        await _release_credits_script(keys=[_hold_key(org_id, token), _key(org_id)], args=[])
    except Exception as e:
        logger.error(f"[billing] release failed for org {org_id}: {e}")


async def release_credits_after(task, org_id: str, token: str | None) -> None:
    """Defer release until the spawned streaming task finishes.

    Streaming requests return a StreamingResponse synchronously after scheduling
    sse_stream_and_finalize as a background task, so chat()'s own finally would
    hand the hold back while the LLM is still streaming.
    """
    try:
        await task
    except Exception:
        pass
    finally:
        await release_credits(org_id, token)
