import aiohttp

from decimal import Decimal
from config import Config

# Lago sits on the request hot path (reserve → sync on cache miss). The shared
# apiservice.fetch allows 600s + retries, which would freeze user requests for
# minutes on a slow Lago. Billing calls its own session with a tight timeout
# and no retries — the caller fails open fast instead.
_LAGO_TIMEOUT = aiohttp.ClientTimeout(total=3, connect=2)


async def get_wallet_balance(org_id: str) -> Decimal:
    """Return the org's current wallet balance in credits.

    Raises Exception if the org has no active wallet yet (residual new-org
    provisioning race, §9.4) — the caller (billing_utils._sync_balance_from_lago)
    treats any failure here the same way (negative-caches and fails open), so no
    dedicated exception type is needed to tell this case apart from other Lago
    failures. Used to reconcile the Redis shadow counter against Lago as ground
    truth (§4).
    """
    url = Config.LAGO_API_URL
    async with aiohttp.ClientSession(timeout=_LAGO_TIMEOUT) as session:
        async with session.get(
            f"{url.rstrip('/')}/wallets",
            headers={
                "Authorization": f"Bearer {Config.LAGO_API_KEY}",
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            },
            params={"external_customer_id": org_id},
        ) as response:
            if response.status >= 300:
                body = await response.text()
                raise Exception(f"Lago wallets lookup failed ({response.status}): {body[:300]}")
            payload = await response.json()

    wallets = (payload or {}).get("wallets", [])
    active = [w for w in wallets if w.get("status") == "active"]
    if not active:
        raise Exception(f"no active Lago wallet for org_id={org_id}")
    balance = active[0].get("credits_ongoing_balance", "0")
    return Decimal(str(balance))
