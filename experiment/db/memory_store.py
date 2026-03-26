"""
JSON-backed persistent LangGraph Store for cross-thread agent memory.

Stores memories in the 'memories' collection of data.json via json_store.
Each memory is keyed by namespace (tuple) + key (str) and holds a dict value.

Usage:
    store = JsonMemoryStore()
    compiled = graph.compile(checkpointer=checkpointer, store=store)

Inside nodes:
    memories = store.search(("agent", agent_id), query="...")
    store.put(("agent", agent_id), "fact_1", {"content": "learned X"})
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)

from db.json_store import _DATA_FILE, _load, _lock, _save


def _ns_key(namespace: tuple[str, ...]) -> str:
    """Convert namespace tuple to a flat string key for JSON storage."""
    return "/".join(namespace)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_str() -> str:
    return _now().isoformat()


class JsonMemoryStore(BaseStore):
    """Persistent LangGraph Store backed by the flat JSON file (data.json)."""

    def _get_collection(self, data: dict) -> dict:
        """Get or create the memories collection."""
        return data.setdefault("memories", {})

    def batch(self, ops: Iterable) -> list:
        """Synchronous batch — delegates to async version via event loop."""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self.abatch(ops))
                return future.result()
        return asyncio.run(self.abatch(ops))

    async def abatch(self, ops: Iterable) -> list:
        """Process a batch of store operations atomically."""
        ops = list(ops)
        results = []

        async with _lock:
            data = _load()
            memories = self._get_collection(data)
            dirty = False

            for op in ops:
                if isinstance(op, GetOp):
                    results.append(self._handle_get(memories, op))
                elif isinstance(op, SearchOp):
                    results.append(self._handle_search(memories, op))
                elif isinstance(op, PutOp):
                    self._handle_put(memories, op)
                    dirty = True
                    results.append(None)
                elif isinstance(op, ListNamespacesOp):
                    results.append(self._handle_list_namespaces(memories, op))
                else:
                    results.append(None)

            if dirty:
                _save(data)

        return results

    def _handle_get(self, memories: dict, op: GetOp) -> Item | None:
        """Get a single item by namespace + key."""
        ns_str = _ns_key(op.namespace)
        ns_data = memories.get(ns_str, {})
        item_data = ns_data.get(op.key)
        if item_data is None:
            return None
        return Item(
            value=item_data["value"],
            key=op.key,
            namespace=op.namespace,
            created_at=datetime.fromisoformat(item_data["created_at"]),
            updated_at=datetime.fromisoformat(item_data["updated_at"]),
        )

    def _handle_search(self, memories: dict, op: SearchOp) -> list[SearchItem]:
        """Search items by namespace prefix, optional filter, and basic text matching."""
        prefix_str = _ns_key(op.namespace_prefix)
        results = []

        for ns_str, ns_items in memories.items():
            if not ns_str.startswith(prefix_str):
                continue

            ns_tuple = tuple(ns_str.split("/"))

            for key, item_data in ns_items.items():
                # Apply filter
                if op.filter:
                    match = all(
                        item_data.get("value", {}).get(k) == v
                        for k, v in op.filter.items()
                    )
                    if not match:
                        continue

                score = 1.0
                # Basic text matching for query (not semantic, but functional)
                if op.query:
                    query_lower = op.query.lower()
                    value_str = str(item_data.get("value", {})).lower()
                    if query_lower in value_str:
                        score = 2.0
                    else:
                        # Simple word overlap scoring
                        query_words = set(query_lower.split())
                        value_words = set(value_str.split())
                        overlap = len(query_words & value_words)
                        if overlap > 0:
                            score = 1.0 + (overlap / len(query_words))
                        else:
                            score = 0.1

                results.append(SearchItem(
                    namespace=ns_tuple,
                    key=key,
                    value=item_data["value"],
                    created_at=datetime.fromisoformat(item_data["created_at"]),
                    updated_at=datetime.fromisoformat(item_data["updated_at"]),
                    score=score,
                ))

        # Sort by score descending, then apply offset/limit
        results.sort(key=lambda x: x.score or 0, reverse=True)
        return results[op.offset : op.offset + op.limit]

    def _handle_put(self, memories: dict, op: PutOp) -> None:
        """Put (create/update/delete) an item."""
        ns_str = _ns_key(op.namespace)

        if op.value is None:
            # Delete
            if ns_str in memories and op.key in memories[ns_str]:
                del memories[ns_str][op.key]
                if not memories[ns_str]:
                    del memories[ns_str]
            return

        ns_data = memories.setdefault(ns_str, {})
        now = _now_str()
        existing = ns_data.get(op.key)

        ns_data[op.key] = {
            "value": op.value,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }

    def _handle_list_namespaces(
        self, memories: dict, op: ListNamespacesOp
    ) -> list[tuple[str, ...]]:
        """List all unique namespaces, optionally filtered by match conditions."""
        all_ns = set()
        for ns_str in memories:
            ns_tuple = tuple(ns_str.split("/"))
            if op.max_depth is not None:
                ns_tuple = ns_tuple[: op.max_depth]
            all_ns.add(ns_tuple)

        # Apply match conditions if any
        if op.match_conditions:
            filtered = set()
            for ns in all_ns:
                match = True
                for cond in op.match_conditions:
                    idx = cond.path
                    if isinstance(idx, int) and idx < len(ns):
                        if cond.match_type == "prefix":
                            if not ns[idx].startswith(str(cond.path)):
                                match = False
                        elif cond.match_type == "suffix":
                            if not ns[idx].endswith(str(cond.path)):
                                match = False
                if match:
                    filtered.add(ns)
            all_ns = filtered

        result = sorted(all_ns)
        return result[op.offset : op.offset + op.limit]
