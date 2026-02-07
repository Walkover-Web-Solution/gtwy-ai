import json

from fastapi import HTTPException, Request

from src.configs.constant import redis_keys

from ..services.cache_service import find_in_cache, store_in_cache, verify_ttl


async def get_nested_value(request: Request, path):
    """Extract nested value from the request object based on the key path."""
    keys = path.split(".")

    if keys[0] == "body":
        try:
            obj = await request.json()
            keys = keys[1:]
        except Exception:
            return None
    elif keys[0] == "profile":
        obj = request.state.profile
        keys = keys[1:]
    elif keys[0] == "headers":
        obj = request.headers
        keys = keys[1:]
    else:
        return None

    for key in keys:
        if hasattr(obj, key):
            obj = getattr(obj, key)
        elif isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return None
    return obj


async def rate_limit(request: Request, key_path: str, points: int = 40, ttl: int = 60):
    key = await get_nested_value(request, key_path)
    if not key:
        return

    redis_key = f"{redis_keys['rate_limit_']}{key}"
    record = await find_in_cache(redis_key)

    if record:
        ttl = await verify_ttl(redis_key)
        data = json.loads(record)
        count = data["count"]
        if count >= points:
            raise HTTPException(
                status_code=429, detail=f"Too many requests for {key}", headers={"Retry-After": str(ttl)}
            )
        data["count"] += 1
    else:
        data = {"count": 1}

    await store_in_cache(redis_key, data, ttl)
