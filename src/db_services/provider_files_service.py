"""Durable registry for files uploaded to provider Files APIs (OpenAI, Anthropic, ...).

Mongo `provider_files` is the source of truth; Redis (`nd_pfile_...`) is only a
dedup-lookup cache. See docs/file_lifecycle_design.md.

Status machine: uploading -> active -> deleting -> (row hard-deleted)
                                    \-> dead_letter (undeletable on provider; kept as record)
"""

from datetime import datetime, timedelta

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from globals import logger
from models.mongo_connection import db
from src.configs.constant import file_lifecycle_config

providerFilesModel = db["provider_files"]
apikeyCredentialsModel = db["apikeycredentials"]

FILE_STATUS = {
    "uploading": "uploading",
    "active": "active",
    "deleting": "deleting",
    "dead_letter": "dead_letter",
}

# statuses that still reference a (possibly) live provider file — a credential
# owning any of these cannot be hard-deleted yet
LIVE_STATUSES = [FILE_STATUS["uploading"], FILE_STATUS["active"], FILE_STATUS["deleting"]]

_indexes_ensured = False


async def ensure_indexes():
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        await providerFilesModel.create_index(
            [("provider", ASCENDING), ("apikey_object_id", ASCENDING), ("content_sha256", ASCENDING)],
            unique=True,
            partialFilterExpression={"status": {"$in": [FILE_STATUS["uploading"], FILE_STATUS["active"]]}},
            name="uniq_provider_cred_sha_live",
        )
        await providerFilesModel.create_index([("status", ASCENDING), ("expires_at", ASCENDING)], name="status_expires")
        await providerFilesModel.create_index([("org_id", ASCENDING), ("thread_ids", ASCENDING)], name="org_threads")
        await providerFilesModel.create_index([("apikey_object_id", ASCENDING), ("status", ASCENDING)], name="cred_status")
        _indexes_ensured = True
    except Exception as e:
        logger.error(f"provider_files ensure_indexes failed: {e}")


def _new_expiry() -> datetime:
    return datetime.utcnow() + timedelta(seconds=file_lifecycle_config["ttl_seconds"])


async def find_reusable(provider: str, apikey_object_id: str, content_sha256: str) -> dict | None:
    return await providerFilesModel.find_one(
        {
            "provider": provider,
            "apikey_object_id": apikey_object_id,
            "content_sha256": content_sha256,
            "status": {"$in": [FILE_STATUS["uploading"], FILE_STATUS["active"]]},
        }
    )


async def insert_upload_intent(
    *, provider, apikey_object_id, org_id, content_sha256, source_url, filename, mime_type, size_bytes, purpose, thread_id
) -> ObjectId | None:
    """Insert the intent row BEFORE the provider upload (crash-safe registration).

    Returns the new _id, or None when a concurrent request already owns this
    (provider, credential, sha) — caller should re-read via find_reusable.
    """
    now = datetime.utcnow()
    doc = {
        "org_id": org_id,
        "provider": provider,
        "apikey_object_id": apikey_object_id,
        "content_sha256": content_sha256,
        "source_url": source_url,
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "purpose": purpose,
        "file_id": None,
        "status": FILE_STATUS["uploading"],
        "thread_ids": [thread_id] if thread_id else [],
        "usage_count": 0,
        "created_at": now,
        "last_used_at": now,
        "expires_at": _new_expiry(),
        "delete_attempts": 0,
        "last_delete_error": None,
    }
    try:
        result = await providerFilesModel.insert_one(doc)
        return result.inserted_id
    except DuplicateKeyError:
        return None


async def mark_active(_id: ObjectId, file_id: str):
    await providerFilesModel.update_one(
        {"_id": _id},
        {"$set": {"status": FILE_STATUS["active"], "file_id": file_id, "expires_at": _new_expiry()}},
    )


async def touch_usage(_id: ObjectId, thread_id: str | None):
    """Slide the TTL forward and register the referencing thread on reuse."""
    update = {
        "$set": {"last_used_at": datetime.utcnow(), "expires_at": _new_expiry()},
        "$inc": {"usage_count": 1},
    }
    if thread_id:
        update["$addToSet"] = {"thread_ids": thread_id}
    await providerFilesModel.update_one({"_id": _id}, update)


async def touch_usage_by_key(provider: str, apikey_object_id: str, content_sha256: str, thread_id: str | None):
    """touch_usage for Redis-cache hits, where we have the dedup key but not the _id."""
    update = {
        "$set": {"last_used_at": datetime.utcnow(), "expires_at": _new_expiry()},
        "$inc": {"usage_count": 1},
    }
    if thread_id:
        update["$addToSet"] = {"thread_ids": thread_id}
    try:
        await providerFilesModel.update_one(
            {
                "provider": provider,
                "apikey_object_id": apikey_object_id,
                "content_sha256": content_sha256,
                "status": FILE_STATUS["active"],
            },
            update,
        )
    except Exception as e:
        logger.error(f"provider_files touch_usage_by_key failed: {e}")


async def delete_row(_id: ObjectId):
    await providerFilesModel.delete_one({"_id": _id})


async def set_status(_id: ObjectId, status: str, extra: dict | None = None):
    await providerFilesModel.update_one({"_id": _id}, {"$set": {"status": status, **(extra or {})}})


async def revert_to_active_for_retry(_id: ObjectId, error: str):
    """Retryable provider-delete failure: bump attempts, retry on the next pass."""
    await providerFilesModel.update_one(
        {"_id": _id},
        {
            "$set": {"status": FILE_STATUS["active"], "expires_at": datetime.utcnow(), "last_delete_error": error},
            "$inc": {"delete_attempts": 1},
        },
    )


async def list_files_for_org(
    org_id: str, status: str | None = None, limit: int = 50, skip: int = 0
) -> tuple[list[dict], int]:
    """Paginated list of a org's provider files, newest first. Returns (rows, total_count)."""
    query = {"org_id": org_id}
    if status:
        query["status"] = status
    total = await providerFilesModel.count_documents(query)
    cursor = providerFilesModel.find(query).sort("created_at", -1).skip(skip).limit(limit)
    rows = await cursor.to_list(length=limit)
    return rows, total


async def find_expired(limit: int = 200) -> list[dict]:
    cursor = providerFilesModel.find(
        {"status": FILE_STATUS["active"], "expires_at": {"$lte": datetime.utcnow()}}
    ).limit(limit)
    return await cursor.to_list(length=limit)


async def find_stuck(older_than_minutes: int = 30) -> list[dict]:
    """Rows stuck in uploading (crashed before activation) or deleting (crashed mid-delete)."""
    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    cursor = providerFilesModel.find(
        {
            "$or": [
                {"status": FILE_STATUS["uploading"], "created_at": {"$lte": cutoff}},
                {"status": FILE_STATUS["deleting"], "last_used_at": {"$lte": cutoff}},
            ]
        }
    ).limit(500)
    return await cursor.to_list(length=500)


async def expire_files_for_credential(apikey_object_id) -> int:
    """Force-expire every active file owned by a credential (soft-delete drain)."""
    result = await providerFilesModel.update_many(
        {"apikey_object_id": {"$in": _cred_variants(apikey_object_id)}, "status": FILE_STATUS["active"]},
        {"$set": {"expires_at": datetime.utcnow()}},
    )
    return result.modified_count


async def count_live_files_for_credential(apikey_object_id) -> int:
    return await providerFilesModel.count_documents(
        {"apikey_object_id": {"$in": _cred_variants(apikey_object_id)}, "status": {"$in": LIVE_STATUSES}}
    )


async def count_dead_letter_for_credential(apikey_object_id) -> int:
    return await providerFilesModel.count_documents(
        {"apikey_object_id": {"$in": _cred_variants(apikey_object_id)}, "status": FILE_STATUS["dead_letter"]}
    )


def _cred_variants(apikey_object_id) -> list:
    """Registry rows store the credential id as a string; match ObjectId too."""
    variants = [str(apikey_object_id)]
    try:
        variants.append(ObjectId(str(apikey_object_id)))
    except Exception:
        pass
    return variants


# ── apikeycredentials (soft-delete finalization) ────────────────────────────

async def get_credential(apikey_object_id) -> dict | None:
    try:
        return await apikeyCredentialsModel.find_one({"_id": ObjectId(str(apikey_object_id))})
    except Exception:
        return None


async def find_soft_deleted_credentials(limit: int = 100) -> list[dict]:
    # `deletedAt` (camelCase) matches the Node repo's existing soft-delete convention
    cursor = apikeyCredentialsModel.find({"deletedAt": {"$exists": True, "$ne": None}}).limit(limit)
    return await cursor.to_list(length=limit)


async def hard_delete_credential(credential_id: ObjectId):
    await apikeyCredentialsModel.delete_one({"_id": credential_id})
