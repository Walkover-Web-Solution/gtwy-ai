"""
LangChain tools that give an agent access to its own memory store.

Three tools follow the exact flow from the spec:
  search_memory → lightweight list of matches (vector search)
  get_memory    → full MongoDB doc for a specific memory_id
  save_memory   → persist a new (unverified) learning to MongoDB

An admin must verify a memory via the admin API before it becomes
visible in future search_memory results (no unverified chunks are sent to the AI).
"""
import json
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from globals import logger
from src.services.memory.memory_db_service import get_memory, save_memory
from src.services.memory.memory_vector_service import search_memories


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------

class SearchMemoryArgs(BaseModel):
    query: str = Field(description="Natural language description of what knowledge you need")


class GetMemoryArgs(BaseModel):
    memory_id: str = Field(description="The memory_id returned by search_memory")


class SaveMemoryArgs(BaseModel):
    name: str = Field(description="Short title that identifies this learning (e.g. 'Slack order notification setup')")
    description: str = Field(description="One-line summary of what was learned")
    problem: str = Field(description="What the task was and why it was hard or required non-obvious knowledge")
    solution: str = Field(description="Step-by-step how it was solved, including any tricky details")
    references: Optional[list[dict]] = Field(
        default=None,
        description=(
            "Sources used. Each item: {type: 'web_search'|'user_input'|'tool_output', value: '...'}"
        ),
    )


# ---------------------------------------------------------------------------
# Tool factory — scoped to a single agent_id
# ---------------------------------------------------------------------------

def build_memory_tools(agent_id: str) -> list[StructuredTool]:
    """Return the three memory tools bound to *agent_id*."""

    async def _search_memory(query: str) -> str:
        hits = await search_memories(query=query, agent_id=agent_id, top_k=5)
        if not hits:
            return json.dumps([])
        # Return only lightweight fields so the AI can decide relevance cheaply
        return json.dumps([
            {
                "memory_id": h["memory_id"],
                "name": h["name"],
                "description": h["description"],
                "score": round(h["score"], 3),
            }
            for h in hits
        ])

    async def _get_memory(memory_id: str) -> str:
        doc = await get_memory(memory_id)
        if not doc:
            return json.dumps({"error": f"No memory found with id={memory_id}"})
        # Return fields the AI needs — skip internal bookkeeping fields
        return json.dumps({
            "_id": doc["_id"],
            "problem": doc.get("problem", ""),
            "solution": doc.get("solution", ""),
            "references": doc.get("references", []),
        })

    async def _save_memory(
        name: str,
        description: str,
        problem: str,
        solution: str,
        references: Optional[list[dict]] = None,
    ) -> str:
        try:
            memory_id = await save_memory(
                agent_id=agent_id,
                name=name,
                description=description,
                problem=problem,
                solution=solution,
                references=references or [],
            )
            logger.info(f"[Memory] New learning saved: memory_id={memory_id}, agent_id={agent_id}")
            return json.dumps({
                "memory_id": memory_id,
                "status": "unverified",
                "message": "Learning saved. It will become searchable after admin approval.",
            })
        except Exception as e:
            logger.error(f"[Memory] save_memory failed for agent_id={agent_id}: {e}")
            return json.dumps({"error": str(e)})

    search_tool = StructuredTool.from_function(
        coroutine=_search_memory,
        name="search_memory",
        description=(
            "Search past learnings from memory. Returns a list of matches with name, description, "
            "and a relevance score. Use this when your own reasoning is not enough — check if a "
            "similar problem was solved before. Only returns admin-verified memories."
        ),
        args_schema=SearchMemoryArgs,
    )

    get_tool = StructuredTool.from_function(
        coroutine=_get_memory,
        name="get_memory",
        description=(
            "Fetch the full solution details for a specific memory. "
            "Always call search_memory first, then call this with the memory_id of a relevant match."
        ),
        args_schema=GetMemoryArgs,
    )

    save_tool = StructuredTool.from_function(
        coroutine=_save_memory,
        name="save_memory",
        description=(
            "Save a new learning to memory. Call this when you solved a task using knowledge that "
            "your reasoning alone could not have provided — e.g. user input, web search results, "
            "or undocumented tool behavior. Do NOT save trivial or general knowledge."
        ),
        args_schema=SaveMemoryArgs,
    )

    return [search_tool, get_tool, save_tool]
