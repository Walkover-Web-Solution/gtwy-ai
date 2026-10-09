"""Hidden per-org agent backing POST /api/v2/model/ai/completion.

Direct calls carry their whole configuration in the request body, but the
pipeline (billing, thread memory, history) is keyed by bridge_id. Each org gets
one empty agent with bridgeType "direct" that those calls run under; the body
configuration is merged over it by setup_configuration.
"""

import json
from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from models.mongo_connection import db
from src.configs.constant import redis_keys
from src.configs.model_configuration import model_config_document
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.time import with_timeout

configurationModel = db["configurations"]

DIRECT_AGENT_TYPE = "direct"
DIRECT_AGENT_SLUG = "__gtwy_direct_api__"
DIRECT_AGENT_CACHE_TTL = 30 * 24 * 60 * 60  # 30 days


def _direct_agent_doc(org_id: str, user_id: str | None) -> dict:
    # Raw insert gets no Mongoose defaults, so every field the Python/Node
    # readers touch is set explicitly. Dict fields must never be None: getConfiguration
    # calls .items() / {**x} on them.
    now = datetime.now(timezone.utc)
    return {
        "org_id": org_id,
        "user_id": str(user_id) if user_id else "system",
        "name": "Direct API Calls",
        "slugName": DIRECT_AGENT_SLUG,
        "bridgeType": DIRECT_AGENT_TYPE,
        "service": "openai",
        "configuration": {"type": "chat", "prompt": ""},
        "settings": {"response_format": {"type": "default"}},
        "connected_agents": {},
        "agent_info": {},
        "variables_path": {},
        "versions": [],
        "published_version_id": None,
        "folder_id": None,
        "bridge_status": 1,
        # deletedAt is written by the $set in get_or_create_direct_agent.
        "createdAt": now,
        "updatedAt": now,
    }


def fill_model_defaults(service: str, configuration: dict) -> None:
    """Give a direct call every parameter a newly created agent would have.

    Node seeds a new agent with each of its model's parameters set to "default",
    which the pipeline resolves to the model's default value. The direct agent is
    empty, so a required parameter would be missing (Anthropic rejects a call
    without max_tokens). Values sent by the caller are kept.
    """
    model_doc = (model_config_document.get(service) or {}).get(configuration.get("model")) or {}
    for key in model_doc.get("configuration") or {}:
        if key != "model":
            configuration.setdefault(key, "default")


async def get_or_create_direct_agent(org_id: str, user_id: str | None = None) -> str:
    """Return the org's direct agent id, creating it on first use."""
    org_id = str(org_id)
    cache_key = f"{redis_keys['direct_agent_']}{org_id}"

    cached = await find_in_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            return cached.strip('"')

    # No deletedAt filter: a soft-deleted direct agent is restored, not replaced. A
    # second insert would hit the unique (org_id, slugName) index until the 30-day
    # TTL removes the deleted doc, and every direct call of the org would fail.
    query = {"org_id": org_id, "slugName": DIRECT_AGENT_SLUG, "bridgeType": DIRECT_AGENT_TYPE}
    update = {"$setOnInsert": _direct_agent_doc(org_id, user_id), "$set": {"deletedAt": None}}

    async def upsert():
        return await with_timeout(
            configurationModel.find_one_and_update(query, update, upsert=True, return_document=ReturnDocument.AFTER)
        )

    try:
        doc = await upsert()
    except DuplicateKeyError:
        # A concurrent first call won the insert; the same update now matches its doc.
        doc = await upsert()

    if not doc:
        raise RuntimeError(f"Unable to resolve direct agent for org {org_id}")

    agent_id = str(doc["_id"])
    await store_in_cache(cache_key, agent_id, ttl=DIRECT_AGENT_CACHE_TTL)
    return agent_id
