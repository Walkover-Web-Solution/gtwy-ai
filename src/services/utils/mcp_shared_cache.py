"""
Shared-Redis contract between GTWY (this service) and the viasocket-mcp service.

When an agent runs in MCP server-mode, GTWY hands the LLM provider an MCP URL
pointing at viasocket-mcp. viasocket-mcp then needs two things that only GTWY
has: (1) the agent's tool definitions + static ``variables_path`` mapping, and
(2) the per-request ``variables`` used to hide/inject hidden tool fields.

Both are passed across via a dedicated, explicitly-namespaced Redis key family
that BOTH services hard-agree on. We deliberately do NOT use GTWY's internal
``AIMIDDLEWARE_<ENV>_`` cache prefix here (that is private to cache_service);
instead we use the neutral ``gtwy_mcp:`` namespace so the cross-service contract
is unambiguous. The viasocket-mcp side reads these exact key strings.

Keys (ENV must match on both services):
  gtwy_mcp:{ENV}:tools:{org_id}:{bridge_id}  -> tools bundle  (static-ish, per agent)
  gtwy_mcp:{ENV}:vars:{request_token}        -> per-request variables (short TTL)
"""

import json

from config import Config
from globals import logger
from src.services.cache_service import client, make_json_serializable

SHARED_NAMESPACE = "gtwy_mcp"
_ENV = Config.ENVIRONMENT or "default"

# TTLs are generous enough to outlive the provider's full tool loop (provider
# fetches tools/list, then calls tools/call possibly several times) plus tool
# execution time, but short enough that per-request variables don't linger.
TOOLS_BUNDLE_TTL = 900   # 15 min
REQUEST_VARS_TTL = 900   # 15 min


def tools_bundle_key(org_id: str, bridge_id: str) -> str:
    return f"{SHARED_NAMESPACE}:{_ENV}:tools:{org_id}:{bridge_id}"


def request_vars_key(token: str) -> str:
    return f"{SHARED_NAMESPACE}:{_ENV}:vars:{token}"


async def write_mcp_tools_bundle(org_id: str, bridge_id: str, bundle: dict, ttl: int = TOOLS_BUNDLE_TTL) -> bool:
    """Publish the agent's tool bundle (tools + variables_path) for viasocket-mcp."""
    try:
        payload = json.dumps(make_json_serializable(bundle))
        await client.set(tools_bundle_key(org_id, bridge_id), payload, ex=int(ttl))
        return True
    except Exception as e:
        logger.error(f"write_mcp_tools_bundle failed for {org_id}/{bridge_id}: {e}")
        return False


async def write_mcp_request_vars(token: str, payload: dict, ttl: int = REQUEST_VARS_TTL) -> bool:
    """Publish the per-request variables (keyed by the opaque request token)."""
    try:
        data = json.dumps(make_json_serializable(payload))
        await client.set(request_vars_key(token), data, ex=int(ttl))
        return True
    except Exception as e:
        logger.error(f"write_mcp_request_vars failed for token: {e}")
        return False
