from decimal import ROUND_HALF_UP, Decimal

from config import Config
from globals import logger

from src.configs.constant import billing_config
from src.services.billing.lago_service import get_wallet_balance
from src.services.cache_service import REDIS_PREFIX, client

_CREDIT_QUANTUM = Decimal("0.0001")


def build_llm_usage_event(usage: dict, message_id: str, org_id: str, bridge_id: str | None = None) -> dict | None:
    try:
        raw = (usage or {}).get("expectedCost")
        cost_usd = Decimal(str(raw or 0))
        if cost_usd <= 0:
            return None
        rate = Decimal(str(Config.LAGO_CREDIT_RATE_USD))
        credits = (cost_usd / rate).quantize(_CREDIT_QUANTUM, rounding=ROUND_HALF_UP)
        # bridge_id keeps ids unique across agent-to-agent frames, which all share
        # the request-level message_id — without it only the first agent's debit
        # would survive dedup.
        transaction_id = f"llm-usage-{message_id}-{bridge_id}" if bridge_id else f"llm-usage-{message_id}"
    except Exception:
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
_APPLIED_TTL = 7200

_DEBIT_SCRIPT = """
local claimed = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2])
if not claimed then
  return 'DUPLICATE'
end
if redis.call('EXISTS', KEYS[2]) == 0 then
  return 'MISSING'
end
redis.call('INCRBYFLOAT', KEYS[2], -tonumber(ARGV[1]))
return 'OK'
"""

_RESERVE_CREDITS_SCRIPT = """
local key = KEYS[1]
local hold = tonumber(ARGV[1])
local floor = tonumber(ARGV[2])

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
return {'ADMIT', tostring(projected)}
"""

_debit_script = client.register_script(_DEBIT_SCRIPT)
_reserve_credits_script = client.register_script(_RESERVE_CREDITS_SCRIPT)


def _key(org_id: str) -> str:
    return f"{REDIS_PREFIX}{_BALANCE_KEY}{{{org_id}}}"


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _sync_balance_from_lago(org_id: str) -> Decimal:
    balance = await get_wallet_balance(org_id)
    await client.set(_key(org_id), str(balance))
    return balance

async def reserve_credits(org_id: str) -> bool | None:
    """True = hold placed, False = no hold but allow (fail open), None = rejected."""
    key = _key(org_id)
    args = [
        str(billing_config["reserve_credits_per_request"]),
        str(billing_config["reserve_overdraft_floor"]),
    ]

    try:
        status = _decode((await _reserve_credits_script(keys=[key], args=args))[0])
        if status in ("MISSING", "REJECT"):
            await _sync_balance_from_lago(org_id)
            status = _decode((await _reserve_credits_script(keys=[key], args=args))[0])
    except Exception:
        return False

    if status == "REJECT":
        return None
    return status == "ADMIT"

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
            status = _decode(await _debit_script(
                keys=[f"{REDIS_PREFIX}{_APPLIED_KEY}{{{org_id}}}_{transaction_id}", key],
                args=[str(amount), _APPLIED_TTL],
            ))
            if status == "DUPLICATE":
                return
            if status == "MISSING":
                await _sync_balance_from_lago(org_id)
                await client.incrbyfloat(key, float(-amount))
        else:
            if await client.get(key) is None:
                await _sync_balance_from_lago(org_id)
            await client.incrbyfloat(key, float(-amount))
    except Exception:
        pass


async def apply_billing_events(events: dict | list[dict] | None) -> None:
    """Mirror the published billing events into the Redis gate balance."""
    for event in ([events] if isinstance(events, dict) else events or []):
        try:
            await apply_debit(event["org_id"], event["credits"], event.get("transaction_id"))
        except Exception as e:
            logger.error(f"[billing] shadow debit failed for event {event.get('transaction_id')}: {e}")


async def reserve_credits_and_api_key_setup(org_id: str, db_config: dict) -> tuple[bool, dict | None]:
    """Fill in the platform apikey and hold credits for it, in one place.

    setup_api_key leaves apikey as None when a bridge has no key of its own.
    Each bridge decides for ITSELF: with its own key it runs free (wallet=False);
    without one it gets the platform key and wallet=True, so only its usage is
    debited. One hold covers the request whenever any bridge runs on wallet.

    Returns (credit_hold, error). credit_hold True means a hold was actually
    placed — the caller owes a release. Billing is gated per-agent by the
    wallet flag, not by the hold (fail-open still bills).
    """
    configs = db_config.get("bridge_configurations") or {}
    primary = configs.get(db_config.get("primary_bridge_id")) or next(iter(configs.values()), {})

    wallet_needed = False
    for cfg in configs.values():
        if cfg.get("apikey"):
            cfg["wallet"] = False
            continue
        cfg["apikey"] = Config.PLATFORM_API_KEYS.get(cfg.get("service"))
        cfg["wallet"] = bool(cfg["apikey"])  # no platform key for that service → can't run, not billed
        wallet_needed = wallet_needed or cfg["wallet"]

    if not primary.get("apikey"):
        return False, {
            "success": False,
            "error": "Could not find api key or Agent is not Published",
        }

    if not wallet_needed:
        return False, None

    hold = await reserve_credits(org_id)
    if hold is None:
        return False, {
            "success": False,
            "error": "Insufficient credits. Please top up your wallet to continue. For support contact support@gtwy.ai",
            "error_code": "CREDIT_BALANCE_EXHAUSTED",
        }

    return hold, None


async def release_credits(org_id: str) -> None:
    key = _key(org_id)
    hold = Decimal(str(billing_config["reserve_credits_per_request"]))
    try:
        await client.incrbyfloat(key, float(hold))
    except Exception:
        pass


async def release_credits_after(task, org_id: str) -> None:
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
        await release_credits(org_id)
