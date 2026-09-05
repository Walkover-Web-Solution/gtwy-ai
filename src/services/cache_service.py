import json
import time as _time

from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from config import Config
from globals import logger
from src.services.utils.time import log_slow_call, SLOW_CALL_THRESHOLDS, SERVICE_TIMEOUTS

# Initialize the Redis client
client = Redis.from_url(
    Config.REDIS_URI,
    socket_timeout=SERVICE_TIMEOUTS["redis"],  # 10s read/write
    socket_connect_timeout=SERVICE_TIMEOUTS["redis"],  # 10s connect
)

REDIS_PREFIX = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_"
DEFAULT_REDIS_TTL = 172800  # 2 days


async def store_in_cache(identifier: str, data: dict, ttl: int = DEFAULT_REDIS_TTL) -> bool:
    try:
        serialized_data = make_json_serializable(data)
        _t = _time.time()
        result = await client.set(f"{REDIS_PREFIX}{identifier}", json.dumps(serialized_data), ex=int(ttl))
        log_slow_call(f"Redis SET {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return result
    except Exception as e:
        logger.error(f"Error storing in cache: {str(e)}")
        return False


async def find_in_cache(identifier: str) -> str | None:
    try:
        _t = _time.time()
        result = await client.get(f"{REDIS_PREFIX}{identifier}")
        log_slow_call(f"Redis GET {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        if result and isinstance(result, bytes):
            return result.decode("utf-8")
        return result
    except Exception as e:
        logger.error(f"Error finding in cache: {str(e)}")
        return None


async def incr_in_cache(identifier: str, ttl: int = DEFAULT_REDIS_TTL) -> int:
    try:
        key = f"{REDIS_PREFIX}{identifier}"
        _t = _time.time()
        value = await client.incr(key)
        log_slow_call(f"Redis INCR {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        if value == 1:
            await client.expire(key, int(ttl))
        return value
    except Exception as e:
        logger.error(f"Error incrementing in cache: {str(e)}")
        return 0


async def incrbyfloat_in_cache(identifier: str, amount: float) -> float | None:
    """Atomically add ``amount`` to a numeric key and return the new total.

    Atomic, unlike a read-parse-add-write cycle: two requests finishing together
    cannot each read the same starting value and lose one of the increments.
    Requires the key to hold a bare number, which is why the usage counter is
    stored on its own rather than inside a JSON blob.
    """
    try:
        key = f"{REDIS_PREFIX}{identifier}"
        _t = _time.time()
        value = await client.incrbyfloat(key, amount)
        log_slow_call(f"Redis INCRBYFLOAT {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return float(value)
    except Exception as e:
        logger.error(f"Error incrementing float in cache: {str(e)}")
        return None


async def get_float_in_cache(identifier: str) -> float | None:
    """Read a numeric key. ``None`` means the key does not exist."""
    raw = await find_in_cache(identifier)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.error(f"Non-numeric value in cache for {identifier}: {raw!r}")
        return None


async def expire_if_unset(identifier: str, ttl: int) -> bool:
    """Set a TTL only when the key does not already have one.

    Calling EXPIRE on every write restarts the clock, so a key that is written
    often would never expire. ``EXPIRE .. NX`` is one round trip on Redis >= 7.0;
    older servers reject the flag, so we fall back to checking the TTL first.
    """
    try:
        key = f"{REDIS_PREFIX}{identifier}"
        try:
            return bool(await client.expire(key, int(ttl), nx=True))
        except Exception:
            # Server predates EXPIRE .. NX -- only set it when none is present.
            # -1 means "key exists, no expiry"; -2 means the key is already gone.
            if await client.ttl(key) == -1:
                return bool(await client.expire(key, int(ttl)))
            return False
    except Exception as e:
        logger.error(f"Error setting expiry for {identifier}: {str(e)}")
        return False


async def sadd_in_cache(identifier: str, *members: str) -> int:
    """Add members to a Redis set, returning how many were newly added."""
    clean = [str(m) for m in members if m]
    if not clean:
        return 0
    try:
        key = f"{REDIS_PREFIX}{identifier}"
        _t = _time.time()
        added = await client.sadd(key, *clean)
        log_slow_call(f"Redis SADD {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return int(added)
    except Exception as e:
        logger.error(f"Error adding to set {identifier}: {str(e)}")
        return 0


async def smembers_in_cache(identifier: str) -> list[str]:
    """Read every member of a Redis set. Empty list when the key is absent."""
    try:
        key = f"{REDIS_PREFIX}{identifier}"
        _t = _time.time()
        members = await client.smembers(key)
        log_slow_call(f"Redis SMEMBERS {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return [m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in (members or set())]
    except Exception as e:
        logger.error(f"Error reading set {identifier}: {str(e)}")
        return []


async def delete_in_cache(identifiers: str | list[str]) -> bool:
    if not await client.ping():
        return False

    if isinstance(identifiers, str):
        identifiers = [identifiers]

    keys_to_delete = [f"{REDIS_PREFIX}{id}" for id in identifiers]

    try:
        _t = _time.time()
        delete_count = await client.delete(*keys_to_delete)
        log_slow_call(f"Redis DEL {identifiers}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        print(f"Deleted {delete_count} items from cache")
        return True
    except Exception as error:
        logger.error(f"Error during deletion: {str(error)}")
        return False


async def verify_ttl(identifier: str) -> int:
    try:
        if await client.ping():
            key = f"{REDIS_PREFIX}{identifier}"
            _t = _time.time()
            ttl = await client.ttl(key)
            log_slow_call(f"Redis TTL {identifier}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
            print(f"TTL for key {key} is {ttl} seconds")
            return ttl
        else:
            print("Redis client is not ready")
            return -2  # Indicating error
    except Exception as error:
        logger.error(f"Error retrieving TTL from cache: {str(error)}")
        return -1  # Indicating error


async def clear_cache(request) -> JSONResponse:
    try:
        body = await request.json()
        id = body.get("id")
        ids = body.get("ids")

        # Handle single id or array of ids
        if id or ids:
            identifiers = ids if ids else id
            await delete_in_cache(identifiers)

            # Determine response message based on input type
            if isinstance(identifiers, list):
                message = f"Redis Keys cleared successfully ({len(identifiers)} keys)"
            else:
                message = "Redis Key cleared successfully"

            return JSONResponse(status_code=200, content={"message": message})
        elif await client.ping():
            # Scan for keys with the specific prefix
            cursor = b"0"
            while cursor:
                cursor, keys = await client.scan(cursor=cursor, match=f"{REDIS_PREFIX}*")
                if keys:
                    await client.delete(*keys)
            print("Cleared all items with prefix from cache")
            return JSONResponse(status_code=200, content={"message": "Redis cleared successfully"})
        else:
            logger.warning("Redis client is not ready")
            return JSONResponse(status_code=500, content={"message": "Redis client is not ready"})
    except Exception as error:
        logger.error(f"Error clearing cache: {str(error)}")
        return JSONResponse(status_code=500, content={"message": f"Error clearing cache: {error}"})


async def find_in_cache_with_prefix(prefix: str) -> list[str] | None:
    try:
        pattern = f"{REDIS_PREFIX}{prefix}*"
        _t = _time.time()
        keys = await client.keys(pattern)
        values = [json.loads(await client.get(key)) for key in keys]  # Fetch values
        log_slow_call(f"Redis KEYS+MGET {prefix}*", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return values

    except Exception as e:
        logger.error(f"Error finding in cache: {str(e)}")
        return None


def make_json_serializable(data):
    """Recursively converts non-serializable values in a dictionary to strings."""
    if isinstance(data, dict):
        return {k: make_json_serializable(v) for k, v in data.items()}
    elif isinstance(data, list | tuple | set | frozenset):
        return [make_json_serializable(v) for v in data]
    try:
        json.dumps(data)  # Check if serializable
        return data
    except (TypeError, OverflowError):
        return str(data)


async def acquire_lock(lock_key: str, ttl: int = 1800) -> bool:
    """
    Acquire a distributed lock using Redis SET NX EX pattern.

    Args:
        lock_key: Unique identifier for the lock
        ttl: Time-to-live in seconds
               Must be > poll_interval (900s) + max_processing_time
               Default: 1800 seconds = 30 minutes

    Returns:
        True if lock was acquired, False otherwise
    """
    try:
        full_key = f"{REDIS_PREFIX}lock_{lock_key}"
        _t = _time.time()
        # SET NX EX: Set if Not eXists with EXpiration
        result = await client.set(full_key, "locked", nx=True, ex=ttl)
        log_slow_call(f"Redis SETNX lock_{lock_key}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return result is not None
    except Exception as e:
        logger.error(f"Error acquiring lock for {lock_key}: {str(e)}")
        return False


async def release_lock(lock_key: str) -> bool:
    """
    Release a distributed lock.

    Args:
        lock_key: Unique identifier for the lock

    Returns:
        True if lock was released, False otherwise
    """
    try:
        full_key = f"{REDIS_PREFIX}lock_{lock_key}"
        _t = _time.time()
        result = await client.delete(full_key)
        log_slow_call(f"Redis DEL lock_{lock_key}", _time.time() - _t, SLOW_CALL_THRESHOLDS["redis"])
        return result > 0
    except Exception as e:
        logger.error(f"Error releasing lock for {lock_key}: {str(e)}")
        return False


__all__ = [
    "delete_in_cache",
    "store_in_cache",
    "find_in_cache",
    "find_in_cache_with_prefix",
    "verify_ttl",
    "clear_cache",
    "acquire_lock",
    "release_lock",
]
