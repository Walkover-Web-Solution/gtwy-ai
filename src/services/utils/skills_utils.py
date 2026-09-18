import asyncio
import json
from typing import Any

import jwt

from config import Config
from src.configs.constant import SKILL_TOOL_TYPE, redis_keys
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.apiservice import fetch
from src.services.utils.logger import logger

# Short TTL so a renamed skill surfaces quickly; the payload is tiny.
ORG_SKILLS_CACHE_TTL = 300

# Content is bigger and changes rarely, so it can be cached longer.
SKILL_CONTENT_CACHE_TTL = 1800

# The tool loop is blocked while this runs, so cap it.
SKILL_FETCH_TIMEOUT_SECONDS = 20


def _create_token(org_id: str | None, user_id: str | None) -> str | None:
    """Mint the {userId, orgId} JWT the skill service expects. The only place we sign one."""
    if not Config.MCP_JWT_SECRET:
        logger.warning("MCP_JWT_SECRET is not configured; skills cannot be fetched")
        return None
    try:
        return jwt.encode({"userId": user_id, "orgId": org_id}, Config.MCP_JWT_SECRET, algorithm="HS256")
    except Exception as exc:
        logger.error(f"Failed to sign skill service token for org {org_id}: {exc}")
        return None


def _auth_headers(token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _parse_skill_list(payload: Any) -> list[dict[str, Any]]:
    """Pull the skill array out of the upstream {success, message, data} envelope."""
    if isinstance(payload, dict):
        payload = payload.get("data")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _parse_skill(payload: Any) -> dict[str, Any] | None:
    """Pull one skill out of the envelope. None unless it actually looks like a skill,
    so an unexpected 200 body is not cached as a blank skill."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict) or not ("content" in payload or "_id" in payload):
        return None
    return payload


async def fetch_org_skills(org_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
    """Return the org's skills as [{_id, name, description}, ...]. Never raises - a failure just means no skills."""
    token = _create_token(org_id, user_id)
    if not token:
        return []

    cache_key = f"{redis_keys['org_skills_']}{org_id}"
    cached = await find_in_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"fetch_org_skills: bad cache entry for org {org_id}: {exc}")

    try:
        response, _ = await fetch(Config.SKILL_API_URL, "GET", _auth_headers(token), None, None)
    except Exception as exc:
        logger.error(f"fetch_org_skills failed for org {org_id}: {exc}")
        return []

    skills = [
        {
            "_id": str(skill.get("_id") or skill.get("id") or ""),
            "name": skill.get("name") or "",
            "description": skill.get("description") or "",
        }
        for skill in _parse_skill_list(response)
    ]
    skills = [skill for skill in skills if skill["_id"]]

    await store_in_cache(cache_key, skills, ORG_SKILLS_CACHE_TTL)
    return skills


def get_attached_skills(connected_tools: Any, org_skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the agent's skills out of connected_tools, dropping ids that no longer exist."""
    if not isinstance(connected_tools, list) or not org_skills:
        return []

    attached_ids = [
        str(tool.get("id"))
        for tool in connected_tools
        if isinstance(tool, dict) and tool.get("type") == SKILL_TOOL_TYPE and tool.get("id")
    ]
    if not attached_ids:
        return []

    skills_by_id = {skill["_id"]: skill for skill in org_skills}
    return [skills_by_id[skill_id] for skill_id in attached_ids if skill_id in skills_by_id]


async def _fetch_content(skill_id: str, token: str) -> dict[str, Any]:
    cache_key = f"{redis_keys['skill_content_']}{skill_id}"
    cached = await find_in_cache(cache_key)
    if cached:
        try:
            return {"response": json.loads(cached), "metadata": {"type": "skill", "skill_id": skill_id}, "status": 1}
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"fetch_skill_content: bad cache entry for skill {skill_id}: {exc}")

    response, _ = await fetch(f"{Config.SKILL_API_URL}/{skill_id}", "GET", _auth_headers(token), None, None)
    skill = _parse_skill(response)
    if not skill:
        return {
            "response": {"error": f"Skill '{skill_id}' returned no data"},
            "metadata": {"type": "skill", "skill_id": skill_id},
            "status": 0,
        }

    content = {
        "name": skill.get("name") or "",
        "description": skill.get("description") or "",
        "content": skill.get("content") or "",
    }
    await store_in_cache(cache_key, content, SKILL_CONTENT_CACHE_TTL)
    return {"response": content, "metadata": {"type": "skill", "skill_id": skill_id}, "status": 1}


async def fetch_skill_content(skill_id: str | None, org_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """Executor for load_skill. Always returns the tool envelope and never raises."""
    if not skill_id:
        return {
            "response": {"error": "skill_id is required to load a skill"},
            "metadata": {"type": "skill"},
            "status": 0,
        }

    token = _create_token(org_id, user_id)
    if not token:
        return {
            "response": {"error": "Skills are not configured on this gateway"},
            "metadata": {"type": "skill", "skill_id": skill_id},
            "status": 0,
        }

    try:
        return await asyncio.wait_for(_fetch_content(skill_id, token), timeout=SKILL_FETCH_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.error(f"fetch_skill_content timed out for skill {skill_id} (org {org_id})")
        return {
            "response": {"error": f"Loading skill '{skill_id}' timed out"},
            "metadata": {"type": "skill", "skill_id": skill_id},
            "status": 0,
        }
    except Exception as exc:
        logger.error(f"fetch_skill_content failed for skill {skill_id} (org {org_id}): {exc}")
        return {
            "response": {"error": str(exc)},
            "metadata": {"type": "skill", "skill_id": skill_id},
            "status": 0,
        }
