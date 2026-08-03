from config import Config
from globals import logger
from src.services.cache_service import client

BLOCKED_ORGS_KEY = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_blocked_orgs"

async def is_org_blocked(org_id) -> bool:
    if org_id is None:
        return False
    try:
        return bool(await client.sismember(BLOCKED_ORGS_KEY, str(org_id)))
    except Exception:
        return False
