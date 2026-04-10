from typing import Dict

_tool_counts: Dict[str, int] = {}


def init_tool_count(message_id: str, limit: int) -> None:
    _tool_counts[message_id] = int(limit or 3)


def decrement_tool_count(message_id: str) -> int:
    if message_id not in _tool_counts:
        return 0

    _tool_counts[message_id] -= 1
    return _tool_counts[message_id]


def cleanup_tool_count(message_id: str) -> None:
    _tool_counts.pop(message_id, None)
