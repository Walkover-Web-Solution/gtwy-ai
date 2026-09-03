"""Anthropic explicit prompt caching.

Uses explicit caching for system prompt blocks and tools. This provides
granular control over what gets cached:
- Explicitly caching static prefix (shared across users/requests)
- Explicitly caching middle variables (only re-cached when variables change)
- Explicitly caching static suffix (shared across users/requests)
- NOT caching last variable (e.g., date/time) - changes too frequently
- Explicitly caching tools (shared across users/requests)

Cache lifetime: 5 minutes (ephemeral), refreshed free on every hit.
Pricing: Cache writes at 1.25x input price, cache reads at 0.1x input price.
"""

CACHE_CONTROL = {"type": "ephemeral"}


def _strip_marks(blocks):
    """Remove existing cache_control marks to stay within breakpoint limits."""
    for block in blocks:
        if isinstance(block, dict):
            block.pop("cache_control", None)


def apply_anthropic_cache_control(configuration):
    """Apply explicit caching: on system blocks (except last) and tools.

    Mutates and returns ``configuration``. Called before every Anthropic API hit.

    Strategy:
    - If system is split into blocks, cache all except the last block (usually a variable):
      - 1 block (no variables): cache the single block if it's static
      - 2 blocks (1 variable): cache block 1 (static), skip block 2 (variable)
      - 3-4 blocks (2+ variables): cache all except last block (variable)
    - Explicitly cache tools (shared across users/requests)
    - Total breakpoints: system blocks (cached count) + 1 for tools = max 4
    """
    if not isinstance(configuration, dict):
        return configuration

    system = configuration.get("system")
    tools = configuration.get("tools")

    # If system is split into blocks, use explicit caching on all except last
    if isinstance(system, list) and system:
        _strip_marks(system)
        # Mark all system blocks except the last one with explicit cache_control
        for i, block in enumerate(system):
            # Skip the last block (usually a variable like date/time)
            if i == len(system) - 1:
                break
            if isinstance(block, dict) and (block.get("text") or "").strip():
                block["cache_control"] = CACHE_CONTROL

    # Explicitly cache tools (single cache key on last tool)
    if isinstance(tools, list) and tools:
        _strip_marks(tools)
        # Add cache_control to the last tool only
        # This uses 1 breakpoint for the entire tools array
        if tools:
            last_tool = tools[-1]
            if isinstance(last_tool, dict):
                last_tool["cache_control"] = CACHE_CONTROL

    return configuration
