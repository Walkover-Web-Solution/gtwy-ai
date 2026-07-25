"""
Register get_gpt_memory at request time from live GPT memory pages.

Flow (every chat request when gpt_memory is enabled):
  1. prepare_prompt → get_gpt_memory (cache / Sokt plug) → parse_memory
  2. attach_gpt_memory_tool → if pages/keys exist: add tool + prompt inventory
     if no memory: add nothing
  3. LLM may call get_gpt_memory → call_get_gpt_memory reads args, returns pages
"""

from __future__ import annotations

from src.configs.constant import inbuild_tools
from src.services.utils.gpt_memory import get_memory_page_keys, summarize_memory_for_prompt

GPT_MEMORY_TOOL_NAME = inbuild_tools["get_gpt_memory"]
GPT_MEMORY_PROMPT_START = "<!--gpt_memory_tool_prompt_start-->"
GPT_MEMORY_PROMPT_END = "<!--gpt_memory_tool_prompt_end-->"


def build_get_gpt_memory_tool(page_keys: list[str]) -> dict:
    """Internal function-tool schema (same shape as Gtwy_Web_Search / RAG)."""
    keys = [str(k) for k in page_keys]
    keys_list = ", ".join(keys)
    return {
        "type": "function",
        "name": GPT_MEMORY_TOOL_NAME,
        "description": (
            "Fetch long-term GPT memory pages for this thread. "
            f"Available page keys: {keys_list}. "
            "Request only the keys you need; set all=true only when you need every page."
        ),
        "properties": {
            "keys": {
                "description": (
                    "Page keys to return. Choose from: "
                    + keys_list
                    + ". Ignored when all is true."
                ),
                "type": "array",
                "items": {"type": "string", "enum": keys},
                "enum": [],
                "required": [],
                "parameter": {},
            },
            "all": {
                "description": "If true, return every memory page. When true, keys is ignored.",
                "type": "boolean",
                "enum": [],
                "required": [],
                "parameter": {},
            },
        },
        "required": [],
    }


def build_gpt_memory_tool_prompt(memory: dict) -> str:
    """Prompt block listing live keys + short data outlines (not full payloads)."""
    inventory = summarize_memory_for_prompt(memory)
    page_keys = [item["key"] for item in inventory]
    keys_text = ", ".join(page_keys)
    lines = "\n".join(f"- `{item['key']}`: {item['outline']}" for item in inventory)
    example_key = page_keys[0]

    return (
        f"\n\n{GPT_MEMORY_PROMPT_START}\n"
        f"Long-term memory is available via the tool `{GPT_MEMORY_TOOL_NAME}`.\n"
        f"Available page keys: {keys_text}.\n"
        f"Memory pages currently stored:\n{lines}\n"
        "Page keys/structure come from stored GPT memory and may change over time.\n"
        f"- Call `{GPT_MEMORY_TOOL_NAME}` with keys like [\"{example_key}\"] for specific pages.\n"
        f"- Call with all=true only when you need the full memory.\n"
        "- Do not invent memory; only use tool results.\n"
        "- Prefer fetching only the keys you need.\n"
        "- You only have the latest previous conversation in context; "
        "use the memory tool for older context.\n"
        f"{GPT_MEMORY_PROMPT_END}\n"
    )


def _strip_previous_memory_prompt(prompt: str) -> str:
    if not prompt:
        return ""
    start = prompt.find(GPT_MEMORY_PROMPT_START)
    if start == -1:
        legacy = "Long-term memory is available via the tool"
        idx = prompt.find(legacy)
        if idx == -1 or GPT_MEMORY_TOOL_NAME not in prompt[idx:]:
            return prompt
        return prompt[:idx].rstrip()

    end = prompt.find(GPT_MEMORY_PROMPT_END)
    if end == -1:
        return prompt[:start].rstrip()
    end += len(GPT_MEMORY_PROMPT_END)
    return (prompt[:start] + prompt[end:]).strip()


def _remove_existing_tool(tools: list, mapping: dict) -> None:
    tools[:] = [
        t for t in tools if not (isinstance(t, dict) and t.get("name") == GPT_MEMORY_TOOL_NAME)
    ]
    mapping.pop(GPT_MEMORY_TOOL_NAME, None)


def attach_gpt_memory_tool(parsed_data: dict, memory: dict | None) -> None:
    """
    After memory is loaded for this request:
      - pages/keys exist → register get_gpt_memory + refresh prompt inventory
      - no memory / empty pages → do not add tool or prompt text
    """
    if not parsed_data.get("gpt_memory"):
        return

    configuration = parsed_data.setdefault("configuration", {})
    tools = configuration.setdefault("tools", [])
    mapping = parsed_data.setdefault("tool_id_and_name_mapping", {})

    # Always clear previous registration (keys can change between turns)
    _remove_existing_tool(tools, mapping)
    configuration["prompt"] = _strip_previous_memory_prompt(configuration.get("prompt") or "")

    page_keys = get_memory_page_keys(memory)
    if not page_keys:
        # No GPT memory for this thread — do not expose the tool or extra prompt text
        return

    tools.append(build_get_gpt_memory_tool(page_keys))
    mapping[GPT_MEMORY_TOOL_NAME] = {
        "type": GPT_MEMORY_TOOL_NAME,
        "name": GPT_MEMORY_TOOL_NAME,
    }
    configuration["prompt"] = (configuration.get("prompt") or "") + build_gpt_memory_tool_prompt(
        memory
    )
