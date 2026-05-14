import uuid
from datetime import datetime, timezone

from models.mongo_connection import db

_collection = db["agent_learning_memories"]


def _new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


async def save_memory(
    agent_id: str,
    name: str,
    description: str,
    problem: str,
    solution: str,
    references: list[dict],
) -> str:
    memory_id = _new_memory_id()
    doc = {
        "_id": memory_id,
        "agent_id": agent_id,
        "status": "unverified",
        "name": name,
        "description": description,
        "problem": problem,
        "solution": solution,
        "references": references or [],
        "created_at": datetime.now(timezone.utc),
    }
    await _collection.insert_one(doc)
    return memory_id


async def get_memory(memory_id: str) -> dict | None:
    doc = await _collection.find_one({"_id": memory_id})
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


async def list_memories(
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    query: dict = {}
    if agent_id:
        query["agent_id"] = agent_id
    if status:
        query["status"] = status

    cursor = _collection.find(query).sort("created_at", -1).limit(limit)
    docs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        docs.append(doc)
    return docs


async def set_memory_status(memory_id: str, status: str) -> bool:
    result = await _collection.update_one(
        {"_id": memory_id},
        {"$set": {"status": status, "verified_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count > 0


async def delete_memory(memory_id: str) -> bool:
    result = await _collection.delete_one({"_id": memory_id})
    return result.deleted_count > 0
