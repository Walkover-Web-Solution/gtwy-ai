import json
from typing import Union, List
from config import Config
from redis.asyncio import Redis
from fastapi.responses import JSONResponse
from globals import *

# Initialize the Redis client
client = Redis.from_url(Config.REDIS_URI)  # Adjust these parameters as needed

REDIS_PREFIX = 'AIMIDDLEWARE_'
DEFAULT_REDIS_TTL = 172800  # 2 days

async def store_in_cache(identifier: str, data: dict, ttl: int = DEFAULT_REDIS_TTL) -> bool:
    try:
        serialized_data = make_json_serializable(data)
        return await client.set(f"{REDIS_PREFIX}{identifier}", json.dumps(serialized_data), ex=int(ttl))
    except Exception as e:
        logger.error(f"Error storing in cache: {str(e)}")
        return False

async def find_in_cache(identifier: str) -> Union[str, None]:
    try:
        result = await client.get(f"{REDIS_PREFIX}{identifier}")
        if result and isinstance(result, bytes):
            return result.decode('utf-8')
        return result
    except Exception as e:
        logger.error(f"Error finding in cache: {str(e)}")
        return None
        
async def delete_in_cache(identifiers: Union[str, List[str]]) -> bool:
    if not await client.ping():
        return False
    
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    
    keys_to_delete = [f"{REDIS_PREFIX}{id}" for id in identifiers]

    try:
        delete_count = await client.delete(*keys_to_delete)
        print(f"Deleted {delete_count} items from cache")
        return True
    except Exception as error:
        logger.error(f"Error during deletion: {str(error)}")
        return False

async def verify_ttl(identifier: str) -> int:
    try:
        if await client.ping():
            key = f"{REDIS_PREFIX}{identifier}"
            ttl = await client.ttl(key)
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
        id = body.get('id')
        ids = body.get('ids')
        
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
            cursor = b'0'
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

async def find_in_cache_with_prefix(prefix: str) -> Union[List[str], None]:
    try:
        pattern = f"{REDIS_PREFIX}{prefix}*"
        keys = await client.keys(pattern)
        values = [json.loads(await client.get(key)) for key in keys]  # Fetch values
        return values
    
    except Exception as e:
        logger.error(f"Error finding in cache: {str(e)}")
        return None
   
def make_json_serializable(data):
    """Recursively converts non-serializable values in a dictionary to strings."""
    if isinstance(data, dict):
        return {k: make_json_serializable(v) for k, v in data.items()}
    elif isinstance(data, (list, tuple, set, frozenset)):
        return [make_json_serializable(v) for v in data]
    try:
        json.dumps(data)  # Check if serializable
        return data
    except (TypeError, OverflowError):
        return str(data)

async def acquire_lock(lock_key: str, ttl: int = 600) -> bool:
    """
    Acquire a distributed lock using Redis SET NX EX pattern.
    
    Args:
        lock_key: Unique identifier for the lock
        ttl: Time-to-live in seconds (default: 600 seconds = 10 minutes)
        
    Returns:
        True if lock was acquired, False otherwise
    """
    try:
        full_key = f"{REDIS_PREFIX}lock_{lock_key}"
        # SET NX EX: Set if Not eXists with EXpiration
        result = await client.set(full_key, "locked", nx=True, ex=ttl)
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
        result = await client.delete(full_key)
        return result > 0
    except Exception as e:
        logger.error(f"Error releasing lock for {lock_key}: {str(e)}")
        return False

__all__ = ['delete_in_cache', 'store_in_cache', 'find_in_cache', 'find_in_cache_and_expire', 'store_in_cache_permanent_until_read', 'verify_ttl', 'clear_cache', 'acquire_lock', 'release_lock']
