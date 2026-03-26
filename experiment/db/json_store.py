"""
Flat JSON file store — replaces MongoDB for all DB operations.
All data is kept in experiment/data.json.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data.json")
_DATA_FILE = os.path.normpath(_DATA_FILE)

_lock = asyncio.Lock()

_DEFAULT = {
    "agents": {},
    "tools": {},
    "sessions": {},
    "a2a_registry": {},
    "memories": {},
}


# ──────────────────────────────────────────────
# Low-level load / save
# ──────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(_DATA_FILE):
        return {k: {} for k in _DEFAULT}
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in _DEFAULT:
            data.setdefault(key, {})
        return data
    except (json.JSONDecodeError, OSError):
        return {k: {} for k in _DEFAULT}


def _save(data: dict):
    with open(_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Public async helpers used by DB services
# ──────────────────────────────────────────────

async def store_get(collection: str, key: str) -> dict | None:
    async with _lock:
        data = _load()
        return data.get(collection, {}).get(key)


async def store_list(collection: str, filter_fn=None) -> list[dict]:
    async with _lock:
        data = _load()
        items = list(data.get(collection, {}).values())
        if filter_fn:
            items = [i for i in items if filter_fn(i)]
        return items


async def store_put(collection: str, key: str, doc: dict) -> dict:
    async with _lock:
        data = _load()
        data.setdefault(collection, {})[key] = doc
        _save(data)
        return doc


async def store_update(collection: str, key: str, updates: dict) -> dict | None:
    async with _lock:
        data = _load()
        col = data.setdefault(collection, {})
        if key not in col:
            return None
        col[key].update(updates)
        _save(data)
        return col[key]


async def store_delete(collection: str, key: str) -> bool:
    async with _lock:
        data = _load()
        if key in data.get(collection, {}):
            del data[collection][key]
            _save(data)
            return True
        return False


async def store_find_one(collection: str, filter_fn) -> dict | None:
    async with _lock:
        data = _load()
        for doc in data.get(collection, {}).values():
            if filter_fn(doc):
                return doc
        return None


async def init_store():
    """Ensure data file exists."""
    if not os.path.exists(_DATA_FILE):
        _save({k: {} for k in _DEFAULT})
    print(f"JSON store ready: {_DATA_FILE}")


def now_str() -> str:
    return _now_str()
