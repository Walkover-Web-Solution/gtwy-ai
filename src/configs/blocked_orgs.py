from config import Config
from globals import logger
from src.services.cache_service import client
from src.configs.constant import redis_keys

BLOCKED_ORGS_KEY = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_{redis_keys['blocked_orgs_']}"

async def is_org_blocked(org_id) -> bool:
    if org_id is None:
        return False
    try:
        return bool(await client.sismember(BLOCKED_ORGS_KEY, str(org_id)))
    except Exception:
        return False

# Members are f"{org_id}_{user_id}", written by gtwy-node's block_org/user API.
BLOCKED_USERS_KEY = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_{redis_keys['blocked_users_']}"

async def is_user_blocked(org_id, user_id) -> bool:
    if org_id is None or user_id is None:
        return False
    try:
        return bool(await client.sismember(BLOCKED_USERS_KEY, f"{org_id}_{user_id}"))
    except Exception:
        return False
