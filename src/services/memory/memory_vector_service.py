from config import Config
from globals import logger
from src.services.utils.apiservice import fetch

_HIPPOCAMPUS_BASE = "http://hippocampus.gtwy.ai"
_SEARCH_URL = f"{_HIPPOCAMPUS_BASE}/search"
_INGEST_URL = f"{_HIPPOCAMPUS_BASE}/ingest"


def _headers() -> dict:
    return {
        "x-api-key": Config.HIPPOCAMPUS_API_KEY,
        "Content-Type": "application/json",
    }


def _collection_id() -> str | None:
    return getattr(Config, "AGENT_MEMORY_HIPPOCAMPUS_COLLECTION_ID", None) or Config.HIPPOCAMPUS_COLLECTION_ID


async def search_memories(query: str, agent_id: str, top_k: int = 5) -> list[dict]:
    collection = _collection_id()
    if not Config.HIPPOCAMPUS_API_KEY or not collection:
        logger.warning("Memory vector search skipped: missing HIPPOCAMPUS_API_KEY or AGENT_MEMORY_HIPPOCAMPUS_COLLECTION_ID")
        return []

    payload = {
        "query": query,
        "ownerId": agent_id,
        "collectionId": collection,
        "top_k": top_k,
        "minScore": 0.5,
        "filter": {"is_verified": True},
    }

    try:
        response_data, _ = await fetch(url=_SEARCH_URL, method="POST", headers=_headers(), json_body=payload)
        results = (response_data or {}).get("result") or []
        hits = []
        for r in results:
            meta = r.get("payload") or r.get("metadata") or {}
            hits.append({
                "memory_id": meta.get("memory_id") or meta.get("resourceId"),
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "score": r.get("score", 0.0),
            })
        return hits
    except Exception as e:
        logger.error(f"Memory vector search failed: {e}")
        return []


async def index_memory(memory_id: str, agent_id: str, name: str, description: str) -> bool:
    collection = _collection_id()
    if not Config.HIPPOCAMPUS_API_KEY or not collection:
        logger.warning("Memory vector index skipped: missing HIPPOCAMPUS_API_KEY or AGENT_MEMORY_HIPPOCAMPUS_COLLECTION_ID")
        return False

    # Embed name + description together so search finds it by semantic similarity
    content = f"{name}. {description}"
    payload = {
        "ownerId": agent_id,
        "collectionId": collection,
        "resourceId": memory_id,
        "content": content,
        "metadata": {
            "name": name,
            "description": description,
            "agent_id": agent_id,
            "memory_id": memory_id,
            "is_verified": True,
        },
    }

    try:
        await fetch(url=_INGEST_URL, method="POST", headers=_headers(), json_body=payload)
        logger.info(f"Memory indexed in vector DB: memory_id={memory_id}")
        return True
    except Exception as e:
        logger.error(f"Memory vector index failed for memory_id={memory_id}: {e}")
        return False


async def delete_memory_from_index(memory_id: str, agent_id: str) -> bool:
    _DELETE_URL = f"{_HIPPOCAMPUS_BASE}/delete"
    collection = _collection_id()
    if not Config.HIPPOCAMPUS_API_KEY or not collection:
        return False

    payload = {
        "resourceId": memory_id,
        "ownerId": agent_id,
        "collectionId": collection,
    }
    try:
        await fetch(url=_DELETE_URL, method="DELETE", headers=_headers(), json_body=payload)
        return True
    except Exception as e:
        logger.error(f"Memory vector delete failed for memory_id={memory_id}: {e}")
        return False
