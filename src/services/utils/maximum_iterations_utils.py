import uuid
from typing import Dict, Optional

_tool_counts: Dict[str, int] = {}
_tool_owners: Dict[str, str] = {}


def build_tool_count_key(
    agent_id: Optional[str],
    message_id: Optional[str],
) -> str:
    """Build a per-agent tool-call counter key.

    The key scopes the iteration limit to a single agent within a
    single request (message) so that child agents (agent-as-tool)
    maintain their own independent counters and are not restricted
    by the parent's remaining budget.
    """
    return f"{agent_id}-{message_id}"


def init_tool_count(key: str, limit: int) -> Optional[str]:
    """Initialize the counter for ``key`` if not already initialized.

    Returns a unique owner token to the *first* caller that successfully
    initializes the counter. Subsequent callers (e.g. child agents that
    recursively invoke the same parent agent as a tool) get ``None`` and
    therefore cannot later wipe the original counter via ``cleanup_tool_count``.
    """
    if key in _tool_counts:
        return None
    _tool_counts[key] = int(limit or 3)
    token = uuid.uuid4().hex
    _tool_owners[key] = token
    return token


def decrement_tool_count(key: str) -> int:
    if key not in _tool_counts:
        return 0

    _tool_counts[key] -= 1
    return _tool_counts[key]


def get_tool_count(key: str) -> Optional[int]:
    """Return remaining tool-call budget for this key, or None if not initialized."""
    return _tool_counts.get(key)


def cleanup_tool_count(key: Optional[str], owner_token: Optional[str]) -> None:
    """Clean up the counter only if ``owner_token`` matches the initializing owner.

    This prevents recursive child instances (which reuse the same key but did
    not initialize it) from clearing the parent's iteration budget.
    """
    if not key or not owner_token:
        return
    if _tool_owners.get(key) == owner_token:
        _tool_counts.pop(key, None)
        _tool_owners.pop(key, None)
