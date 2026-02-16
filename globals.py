import asyncio
import traceback

from exceptions.bad_request import BadRequestException
from src.services.utils.logger import logger


async def try_catch(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except Exception:
        return None


REDIS_SEMAPHORE = asyncio.Semaphore(200)
MONGO_SEMAPHORE = asyncio.Semaphore(50)

# Global dictionary to track transfer history for each request
# Structure: {request_id: [{'bridge_id': ..., 'history_params': ..., 'dataset': ..., 'version_id': ..., 'thread_info': ...}]}
TRANSFER_HISTORY = {}

__all__ = [
    "logger",
    "BadRequestException",
    "traceback",
    "try_catch",
    "REDIS_SEMAPHORE",
    "MONGO_SEMAPHORE",
    "TRANSFER_HISTORY",
]
