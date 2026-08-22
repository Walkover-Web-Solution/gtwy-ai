from decimal import Decimal
from config import Config

from src.services.utils.apiservice import fetch

async def get_wallet_balance(org_id: str) -> Decimal:
    """Return the org's current wallet balance in credits.

    Raises Exception if the org has no active wallet yet (residual new-org
    provisioning race, §9.4) — the caller (billing_utils._sync_balance_from_lago)
    treats any failure here the same way, so no dedicated exception type is
    needed to tell this case apart from other Lago failures.
    Used to reconcile the Redis shadow counter against Lago as ground truth (§4).
    """
    url = Config.LAGO_API_URL
    response, _ = await fetch(
        f"{url.rstrip('/')}/wallets",
        "GET",
        {
            "Authorization": f"Bearer {Config.LAGO_API_KEY}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        },
        {"external_customer_id": org_id},
        None,
    )
    wallets = (response or {}).get("wallets", [])
    active = [w for w in wallets if w.get("status") == "active"]
    if not active:
        raise Exception(f"no active Lago wallet for org_id={org_id}")
    balance = active[0].get("credits_ongoing_balance", "0")
    return Decimal(str(balance))

