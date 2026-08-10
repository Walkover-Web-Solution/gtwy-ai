import asyncio

from fastapi import HTTPException, Request

from globals import logger
from src.db_services import provider_files_service as registry
from src.services.utils.helper import Helper
from src.services.utils.provider_file_adapters import get_file_adapter

_PROVIDER_STATUS_CONCURRENCY = 5


def _serialize(row: dict, provider_info: dict | None = None) -> dict:
    data = {
        "id": str(row["_id"]),
        "provider": row.get("provider"),
        "file_id": row.get("file_id"),
        "filename": row.get("filename"),
        "mime_type": row.get("mime_type"),
        "size_bytes": row.get("size_bytes"),
        "status": row.get("status"),
        "source_url": row.get("source_url"),
        "thread_ids": row.get("thread_ids") or [],
        "usage_count": row.get("usage_count", 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "last_used_at": row["last_used_at"].isoformat() if row.get("last_used_at") else None,
        "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
    }
    if provider_info is not None:
        data["exists_on_provider"] = provider_info.get("exists")
        data["provider_status"] = provider_info.get("status")
        data["provider_bytes"] = provider_info.get("bytes")
    return data


async def _fetch_provider_status(rows: list[dict]) -> dict[str, dict]:
    """Live-check each row against its provider (e.g. OpenAI's files.retrieve).

    Decrypts each distinct credential once, then fans out with bounded
    concurrency. Never raises — a failed lookup just leaves that row's
    provider fields as unknown (exists=None) instead of breaking the listing.
    """
    candidates = [row for row in rows if row.get("file_id") and row.get("apikey_object_id")]
    if not candidates:
        return {}

    apikeys_by_cred: dict[str, str | None] = {}
    for row in candidates:
        cred_id = row["apikey_object_id"]
        if cred_id in apikeys_by_cred:
            continue
        credential = await registry.get_credential(cred_id)
        if not credential or not credential.get("apikey"):
            apikeys_by_cred[cred_id] = None
            continue
        try:
            apikeys_by_cred[cred_id] = Helper.decrypt(credential["apikey"])
        except Exception as e:
            logger.error(f"provider_files: credential decrypt failed for {cred_id}: {e}")
            apikeys_by_cred[cred_id] = None

    semaphore = asyncio.Semaphore(_PROVIDER_STATUS_CONCURRENCY)
    results: dict[str, dict] = {}

    async def fetch(row):
        row_id = str(row["_id"])
        adapter = get_file_adapter(row["provider"])
        apikey = apikeys_by_cred.get(row["apikey_object_id"])
        if adapter is None or not apikey:
            return
        async with semaphore:
            info = await adapter.retrieve(apikey, row["file_id"])
        if info is not None:
            results[row_id] = info

    await asyncio.gather(*(fetch(row) for row in candidates))
    return results


async def list_org_files(request: Request):
    org_id = request.state.profile.get("org", {}).get("id") if hasattr(request.state, "profile") else None
    if not org_id:
        raise HTTPException(status_code=400, detail={"success": False, "error": "org_id could not be resolved from the token"})

    status = request.query_params.get("status")
    valid_statuses = set(registry.FILE_STATUS.values())
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": f"invalid status filter; must be one of {sorted(valid_statuses)}"},
        )

    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
        skip = max(int(request.query_params.get("skip", 0)), 0)
    except ValueError:
        raise HTTPException(status_code=400, detail={"success": False, "error": "limit/skip must be integers"})

    include_provider_status = request.query_params.get("include_provider_status", "").lower() == "true"

    rows, total = await registry.list_files_for_org(org_id, status=status, limit=limit, skip=skip)

    provider_status_by_id = await _fetch_provider_status(rows) if include_provider_status else {}

    return {
        "success": True,
        "total": total,
        "limit": limit,
        "skip": skip,
        "files": [_serialize(row, provider_status_by_id.get(str(row["_id"]))) for row in rows],
    }
