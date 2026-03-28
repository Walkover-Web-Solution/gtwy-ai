"""
Memory DB service — high-level helpers for managing agent memories.

Uses the JsonMemoryStore (LangGraph Store) under the hood.
Namespace convention: ("agent", agent_id, [thread_id], category)
  - category: "facts", "preferences", "learnings", "session_summaries"
"""

import uuid
from datetime import datetime, timezone

from db.memory_store import JsonMemoryStore

# Shared store instance (same one used by the graph)
_store = None


def get_store() -> JsonMemoryStore:
    """Get the shared memory store instance."""
    global _store
    if _store is None:
        _store = JsonMemoryStore()
    return _store


def set_store(store: JsonMemoryStore):
    """Set the shared store (called from builder to share the same instance)."""
    global _store
    _store = store


async def save_memory(
    agent_id: str,
    content: str,
    category: str = "learnings",
    metadata: dict = None,
    thread_id: str | None = None,
) -> dict:
    """Save a memory for an agent (scoped by optional thread_id)."""
    store = get_store()
    namespace = ("agent", agent_id)
    if thread_id:
        namespace += (thread_id,)
    namespace += (category,)
    key = f"mem_{uuid.uuid4().hex[:12]}"
    value = {
        "content": content,
        "category": category,
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await store.aput(namespace, key, value)
    return {"key": key, "namespace": namespace, "value": value}


async def get_memory(
    agent_id: str,
    key: str,
    category: str = "learnings",
    thread_id: str | None = None,
) -> dict | None:
    """Get a specific memory by key."""
    store = get_store()
    namespace = ("agent", agent_id)
    if thread_id:
        namespace += (thread_id,)
    namespace += (category,)
    item = await store.aget(namespace, key)
    if item is None:
        return None
    return {"key": item.key, "namespace": item.namespace, "value": item.value}


async def search_memories(
    agent_id: str,
    query: str = None,
    category: str = None,
    thread_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search memories for an agent, optionally filtered by category and query."""
    store = get_store()
    # Search across all categories if none specified
    namespace_parts = ["agent", agent_id]
    if thread_id:
        namespace_parts.append(thread_id)
    if category:
        namespace_parts.append(category)
    namespace_prefix = tuple(namespace_parts)
    items = await store.asearch(
        namespace_prefix,
        query=query,
        limit=limit,
    )
    return [
        {
            "key": item.key,
            "namespace": item.namespace,
            "value": item.value,
            "score": item.score,
        }
        for item in items
    ]


async def delete_memory(
    agent_id: str,
    key: str,
    category: str = "learnings",
    thread_id: str | None = None,
) -> bool:
    """Delete a specific memory."""
    store = get_store()
    namespace = ("agent", agent_id)
    if thread_id:
        namespace += (thread_id,)
    namespace += (category,)
    await store.aput(namespace, key, None)  # PutOp with value=None deletes
    return True


async def get_agent_memories_for_prompt(
    agent_id: str,
    goal: str = None,
    limit: int = 10,
    thread_id: str | None = None,
) -> str:
    """Get formatted memories for injection into agent prompts.

    Returns a string block suitable for system prompt injection.
    Searches across all categories, optionally using goal as query for relevance.
    """
    memories = await search_memories(agent_id, query=goal, limit=limit, thread_id=thread_id)
    if not memories:
        return ""

    lines = []
    for mem in memories:
        category = mem["value"].get("category", "general")
        content = mem["value"].get("content", "")
        lines.append(f"- [{category}] {content}")

    return "Relevant memories from past sessions:\n" + "\n".join(lines)


async def save_session_summary(
    agent_id: str,
    session_id: str,
    goal: str,
    summary: str,
    key_facts: list[str] = None,
    thread_id: str | None = None,
) -> list[dict]:
    """Save session summary and extracted facts as memories.

    Called after a session completes to persist learnings for future sessions.
    """
    saved = []

    # Save session summary
    result = await save_memory(
        agent_id=agent_id,
        content=f"Session {session_id}: Goal was '{goal}'. Summary: {summary}",
        category="session_summaries",
        metadata={"session_id": session_id, "goal": goal},
        thread_id=thread_id,
    )
    saved.append(result)

    # Save individual facts
    if key_facts:
        for fact in key_facts:
            result = await save_memory(
                agent_id=agent_id,
                content=fact,
                category="learnings",
                metadata={"source_session": session_id},
                thread_id=thread_id,
            )
            saved.append(result)

    return saved
