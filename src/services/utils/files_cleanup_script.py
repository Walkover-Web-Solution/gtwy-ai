"""Hourly cleanup for provider Files-API uploads.

Responsibilities (see docs/file_lifecycle_design.md §8):
  1. Delete expired files on the provider, then hard-delete their Mongo rows
     and Redis cache entries together.
  2. Sweep rows stuck in uploading/deleting (crashed mid-operation).
  3. Drain soft-deleted API-key credentials (deletedAt set by the Node repo):
     force-expire their files, and hard-delete the credential once no live
     file references remain (dead_letter rows block finalization).

Runs in every gunicorn worker's lifespan, but a Redis SET NX EX lock ensures
only one pass executes per interval across all workers/instances.
"""

import asyncio

import globals as _globals
from globals import logger
from src.configs.constant import file_lifecycle_config, redis_keys
from src.db_services import provider_files_service as registry
from src.services.cache_service import acquire_lock, delete_in_cache
from src.services.utils.helper import Helper
from src.services.utils.provider_file_adapters import DeleteResult, get_file_adapter

_LOCK_KEY = "files_cleanup"
_DELETE_CONCURRENCY = 5
_EXPIRED_BATCH_SIZE = 200


async def files_cleanup_cron():
    await registry.ensure_indexes()
    while _globals.is_ready:
        try:
            if file_lifecycle_config["enabled"]:
                # Lock TTL just under the interval; not released, so exactly one
                # worker/pod runs the pass per interval.
                lock_ttl = max(60, file_lifecycle_config["cleanup_interval_seconds"] - 300)
                if await acquire_lock(_LOCK_KEY, ttl=lock_ttl):
                    await run_cleanup_pass()
        except Exception as e:
            logger.error(f"files cleanup pass failed: {e}")
        await asyncio.sleep(file_lifecycle_config["cleanup_interval_seconds"])
    logger.info("Files cleanup cron stopped — server is shutting down")


async def run_cleanup_pass():
    logger.info("Files cleanup pass starting...")
    await _sweep_stuck_rows()
    await _drain_soft_deleted_credentials()
    await _delete_expired_files()
    await _finalize_soft_deleted_credentials()
    logger.info("Files cleanup pass finished")


async def _sweep_stuck_rows():
    """Rows stuck in uploading/deleting mean a crash mid-operation. Reconciliation
    against the provider's file list is the safety net for any provider-side orphan."""
    try:
        stuck = await registry.find_stuck()
        for row in stuck:
            if row.get("status") == registry.FILE_STATUS["deleting"]:
                # crashed after the provider delete may or may not have happened —
                # retry the delete instead of dropping the pointer
                await registry.revert_to_active_for_retry(row["_id"], "stuck in deleting")
            else:
                await registry.delete_row(row["_id"])
        if stuck:
            logger.info(f"files cleanup: swept {len(stuck)} stuck rows")
    except Exception as e:
        logger.error(f"files cleanup: stuck-row sweep failed: {e}")


async def _drain_soft_deleted_credentials():
    try:
        credentials = await registry.find_soft_deleted_credentials()
        for cred in credentials:
            expired = await registry.expire_files_for_credential(cred["_id"])
            if expired:
                logger.info(f"files cleanup: force-expired {expired} files of soft-deleted apikey {cred['_id']}")
    except Exception as e:
        logger.error(f"files cleanup: soft-deleted credential drain failed: {e}")


async def _delete_expired_files():
    expired = await registry.find_expired(limit=_EXPIRED_BATCH_SIZE)
    if not expired:
        return
    semaphore = asyncio.Semaphore(_DELETE_CONCURRENCY)

    async def process(row):
        async with semaphore:
            await _delete_one_file(row)

    await asyncio.gather(*(process(row) for row in expired), return_exceptions=True)
    logger.info(f"files cleanup: processed {len(expired)} expired files")


async def _delete_one_file(row):
    _id = row["_id"]
    file_id = row.get("file_id")
    provider = row.get("provider")
    adapter = get_file_adapter(provider)

    if not file_id:
        await registry.delete_row(_id)
        return
    if adapter is None:
        await registry.set_status(
            _id, registry.FILE_STATUS["dead_letter"], {"last_delete_error": f"no adapter for provider {provider}"}
        )
        return

    credential = await registry.get_credential(row.get("apikey_object_id"))
    if credential is None or not credential.get("apikey"):
        await registry.set_status(
            _id, registry.FILE_STATUS["dead_letter"], {"last_delete_error": "credential missing — cannot delete on provider"}
        )
        logger.error(f"files cleanup: DEAD LETTER {provider}/{file_id} — credential {row.get('apikey_object_id')} missing")
        return

    try:
        apikey = Helper.decrypt(credential["apikey"])
    except Exception as e:
        await registry.set_status(_id, registry.FILE_STATUS["dead_letter"], {"last_delete_error": f"decrypt failed: {e}"})
        return

    await registry.set_status(_id, registry.FILE_STATUS["deleting"])
    outcome = await adapter.delete(apikey, file_id)

    if outcome in (DeleteResult.OK, DeleteResult.ALREADY_GONE):
        cache_id = (
            f"{redis_keys['provider_file_']}{provider}:{row.get('apikey_object_id')}:{row.get('content_sha256')}"
        )
        await registry.delete_row(_id)
        await delete_in_cache(cache_id)
    elif outcome == DeleteResult.AUTH_FAILED:
        await registry.set_status(_id, registry.FILE_STATUS["dead_letter"], {"last_delete_error": "auth failed (401/403)"})
        logger.error(f"files cleanup: DEAD LETTER {provider}/{file_id} — provider auth failed, key rotated/revoked")
    else:  # retryable
        if row.get("delete_attempts", 0) + 1 >= file_lifecycle_config["delete_max_attempts"]:
            await registry.set_status(
                _id, registry.FILE_STATUS["dead_letter"], {"last_delete_error": "max delete attempts exceeded"}
            )
            logger.error(f"files cleanup: DEAD LETTER {provider}/{file_id} — max delete attempts exceeded")
        else:
            await registry.revert_to_active_for_retry(_id, "retryable provider error")


async def _finalize_soft_deleted_credentials():
    """Hard-delete soft-deleted credentials once nothing live references them.

    dead_letter rows keep the credential alive: the row + credential pair is the
    only remaining record of an undeletable provider file.
    """
    try:
        credentials = await registry.find_soft_deleted_credentials()
        for cred in credentials:
            live = await registry.count_live_files_for_credential(cred["_id"])
            if live > 0:
                continue
            dead = await registry.count_dead_letter_for_credential(cred["_id"])
            if dead > 0:
                logger.error(
                    f"files cleanup: apikey {cred['_id']} kept soft-deleted — {dead} dead-letter file(s) need manual cleanup"
                )
                continue
            await registry.hard_delete_credential(cred["_id"])
            logger.info(f"files cleanup: finalized deletion of apikey credential {cred['_id']}")
    except Exception as e:
        logger.error(f"files cleanup: credential finalization failed: {e}")
