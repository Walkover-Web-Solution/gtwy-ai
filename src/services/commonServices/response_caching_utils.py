from config import Config
from globals import logger
from models.mongo_connection import db
from src.services.utils.apiservice import fetch
from src.services.utils.time import with_timeout

HIPPOCAMPUS_SEARCH_URL = "http://hippocampus.gtwy.ai/search"
agent_memory_collection = db["agent_memories"]


async def get_memories_from_hippocampus(
    user_question: str,
    agent_id: str,
    top_k: int = 1,
    limit: int = 1,
    min_score: float = 0.9,
) -> dict:
    if not Config.HIPPOCAMPUS_API_KEY or not Config.HIPPOCAMPUS_COLLECTION_ID:
        logger.info("Response cache lookup skipped: missing HIPPOCAMPUS_API_KEY or HIPPOCAMPUS_COLLECTION_ID")
        return {"found": False, "resource_id": None, "score": 0.0}

    headers = {
        "x-api-key": Config.HIPPOCAMPUS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "query": user_question,
        "ownerId": agent_id,
        "collectionId": Config.HIPPOCAMPUS_COLLECTION_ID,
        "top_k": top_k,
        "limit": limit,
        "minScore": min_score,
    }

    response_data, _ = await fetch(
        url=HIPPOCAMPUS_SEARCH_URL,
        method="POST",
        headers=headers,
        json_body=payload,
    )

    results = (response_data or {}).get("result") or []
    logger.info(f"Hippocampus cache lookup completed for agent_id={agent_id}, results_count={len(results)}")
    if not results:
        return {"found": False, "resource_id": None, "score": 0.0}

    top_result = results[0] or {}
    payload_obj = top_result.get("payload") or {}
    resource_id = payload_obj.get("resourceId")
    score = top_result.get("score", 0.0)
    logger.info(
        f"Hippocampus top cache candidate for agent_id={agent_id}: resource_id={resource_id}, score={score}"
    )

    return {
        "found": bool(resource_id),
        "resource_id": resource_id,
        "score": score,
    }


async def get_response_using_resourceid(resource_id: str) -> dict:
    if not resource_id:
        logger.info("Mongo cache lookup skipped: empty resource_id")
        return {"found": False, "answer": None}

    memory = await with_timeout(agent_memory_collection.find_one({"resource_id": resource_id}))
    if not memory:
        logger.info(f"Mongo cache miss for resource_id={resource_id}")
        return {"found": False, "answer": None}

    answer = memory.get("original_answer")
    
    answer = answer.strip() if isinstance(answer, str) else None
    logger.info(f"Mongo cache lookup hit for resource_id={resource_id}, has_answer={bool(answer)}")

    return {"found": bool(answer), "answer": answer}
