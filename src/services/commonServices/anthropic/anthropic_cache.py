"""Anthropic prompt-caching breakpoints.

Anthropic has no user-supplied cache key: caching is exact prefix matching over
tools -> system -> messages, with breakpoints marked via
``"cache_control": {"type": "ephemeral"}`` (5-minute TTL, refreshed free on every
hit). Reads bill at 0.1x input price, writes at 1.25x. The API allows at most 4
breakpoints per request; our budget is 1 tools + 1 system + 2 messages.

The markers themselves are not part of the hashed content, so moving them between
tool-loop iterations never invalidates the cache. On a cache read Anthropic walks
backward from a breakpoint up to 20 blocks to find the previous write, which is
how the marker on the newest message connects to the entry written one iteration
earlier; the second message marker is a safety net for turns that append many
blocks at once (e.g. parallel tool calls).

Prefixes below the model's minimum cacheable length (~1024-4096 tokens depending
on model) are silently not cached — no error, just zero cache usage.
"""

import os

CACHE_CONTROL = {"type": "ephemeral"}

# Block types that may legally carry cache_control inside message content.
# Notably excluded: thinking, redacted_thinking, mcp_tool_use, mcp_tool_result,
# server_tool_use, web_search_tool_result.
_MARKABLE_MESSAGE_BLOCK_TYPES = {"text", "image", "tool_use", "tool_result", "document"}
_MAX_MESSAGE_BREAKPOINTS = 2


def _caching_disabled():
    return os.getenv("ANTHROPIC_PROMPT_CACHING_DISABLED", "").lower() in ("1", "true")


def _strip_marks(blocks):
    for block in blocks:
        if isinstance(block, dict):
            block.pop("cache_control", None)


def _is_markable(block):
    if not isinstance(block, dict) or block.get("type") not in _MARKABLE_MESSAGE_BLOCK_TYPES:
        return False
    if block.get("type") == "text" and not (block.get("text") or "").strip():
        return False
    return True


def apply_anthropic_cache_control(configuration):
    """Idempotently place cache_control breakpoints on tools, system and messages.

    Mutates and returns ``configuration``. Called before every Anthropic API hit,
    including each tool-loop iteration, which reuses the same dict — so stale
    marks are stripped before re-marking to stay within the 4-breakpoint limit.
    """
    if not isinstance(configuration, dict) or _caching_disabled():
        return configuration

    tools = configuration.get("tools")
    if isinstance(tools, list) and tools:
        _strip_marks(tools)
        if isinstance(tools[-1], dict):
            tools[-1]["cache_control"] = CACHE_CONTROL

    system = configuration.get("system")
    if isinstance(system, str):
        if system.strip():
            configuration["system"] = [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}]
    elif isinstance(system, list):
        _strip_marks(system)
        # Mark the first non-empty block: with the static/dynamic prompt split the
        # first block is the cross-user static prefix; the dynamic remainder is
        # covered by the message breakpoints downstream.
        for block in system:
            if isinstance(block, dict) and (block.get("text") or "").strip():
                block["cache_control"] = CACHE_CONTROL
                break

    messages = configuration.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                _strip_marks(content)
        marked = 0
        for message in reversed(messages):
            if marked >= _MAX_MESSAGE_BREAKPOINTS:
                break
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if _is_markable(block):
                    block["cache_control"] = CACHE_CONTROL
                    marked += 1
                    break

    return configuration
