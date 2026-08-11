"""Builds the execute_plan meta-tool definition, and (only when at least one
exists) a block of real prior tool-call results for THIS conversation. Tool
name/description/params are never restated here — the AI already gets that
via the normal tools list on the request; this only ever adds *results*.
Only used when parsed_data["mode"] == "auto_tools" — see the injection call
in src/services/commonServices/common.py.
"""

import json

from src.db_services.conversationDbService import get_recent_tool_examples

EXECUTE_PLAN_TOOL_NAME = "execute_plan"

EXECUTE_PLAN_DESCRIPTION = """Use ONLY to chain 2+ tools where one's args need another's result. \
Single/independent call? Call that tool directly instead.

Precondition: you already know each involved tool's name, args, and response shape. Unknown shape? \
Call it directly first — never guess inside a plan; a wrong guess fails the whole plan.

`code`:
async def run(call_tool):
    customer = await call_tool("searchCustomer", {"name": "John"})
    contact = await call_tool("createContact", {"email": customer["email"]})
    return contact

Rules:
- Only call_tool(tool_name, args_dict) is callable, matching a given tool exactly.
- Allowed: variables, if/else, try/except, dict/list get/keys/values/len/indexing. Nothing else — no \
import/eval/exec/open/loops/other functions or methods.
- End with `return <final_value>`.
- Already known from the CURRENT user message? Pass it directly, don't re-fetch via a tool.
- Any "PREVIOUS TOOL CALLS" shown are STALE examples (shape only) — always call_tool for THIS turn's \
real value; never reuse or skip a call because you saw one.
- Never guess a result from a tool's description — it may depend on live data. Always call it.
- A failed call_tool raises — handle via try/except or let it propagate so dependents never run.
"""


def build_execute_plan_tool():
    return {
        "type": "function",
        "name": EXECUTE_PLAN_TOOL_NAME,
        "description": EXECUTE_PLAN_DESCRIPTION,
        "properties": {
            "code": {
                "description": "The async def run(call_tool): ... function body, as a single Python code string.",
                "type": "string",
                "enum": [],
                "required": [],
                "parameter": {},
            },
        },
        "required": ["code"],
    }


async def build_tool_reference_block(org_id, bridge_id, thread_id, sub_thread_id, version_id, tools):
    """Look up a real prior call for each tool IN THIS THREAD/SUB_THREAD.
    Returns "" when none exist yet — tool schemas are already sent via the
    normal tools list, so this block's only job is surfacing past *results*,
    and it should add nothing to the prompt when there aren't any.

    Fetches all tools' examples in one batched call (get_recent_tool_examples)
    instead of one lookup per tool, so the underlying cache/DB read happens
    once per request regardless of how many tools there are.
    """
    tool_names = [t.get("name") for t in tools if t.get("name") and t.get("name") != EXECUTE_PLAN_TOOL_NAME]
    if not tool_names:
        return ""

    examples = await get_recent_tool_examples(org_id, bridge_id, thread_id, sub_thread_id, tool_names, version_id=version_id)
    if not examples:
        return ""

    example_lines = [
        f"- {name}({json.dumps(examples[name].get('args'))}) -> {json.dumps(examples[name].get('response'))[:400]}"
        for name in tool_names
        if name in examples
    ]

    lines = [
        "PREVIOUS TOOL CALLS IN THIS CONVERSATION:",
        "(These are STALE — shown only so you know the shape of each tool's response. They are REAL "
        "results from an earlier call, but the actual value can change each time it's called — it is "
        "NOT guaranteed to be the same now. Always make a NEW tool call to get the current value for "
        "THIS turn — do not reuse these values, and do not skip calling a tool just because you saw a "
        "result for it here.)",
        "",
        *example_lines,
    ]
    return "\n".join(lines)


async def inject_execute_plan_tool(params, parsed_data):
    """Mutate params/parsed_data in place: register execute_plan as a
    callable tool (dispatch type CODE_EXEC, handled in
    baseService/utils.py::process_data_and_run_tools) and append a tool
    reference block (with real historical examples) to the system prompt.
    Only called when parsed_data["mode"] == "auto_tools".

    Field targets mirror src/services/todo/planner_service.py::
    prepare_planner_request exactly, since custom_config (params["customConfig"])
    was already built from parsed_data["configuration"] *before* this runs:
    - tools must be mirrored into BOTH parsed_data["configuration"]["tools"]
      and params["customConfig"]["tools"] (the latter is what actually
      reaches tool_call_formatter at request-formatting time).
    - prompt only needs parsed_data["configuration"]["prompt"] — that's the
      one read when the system message is built.
    """
    configuration = parsed_data.setdefault("configuration", {})
    original_tools = list(configuration.get("tools") or [])

    thread_id = parsed_data.get("thread_id")
    sub_thread_id = parsed_data.get("sub_thread_id") or thread_id
    version_id = parsed_data.get("version_id", "")
    reference_block = await build_tool_reference_block(
        parsed_data.get("org_id"), parsed_data.get("bridge_id"), thread_id, sub_thread_id, version_id, original_tools
    )

    execute_plan_tool = build_execute_plan_tool()
    configuration["tools"] = [*original_tools, execute_plan_tool]

    custom_config = params.get("customConfig")
    if isinstance(custom_config, dict):
        custom_config["tools"] = [*(custom_config.get("tools") or []), execute_plan_tool]

    tool_id_and_name_mapping = dict(params.get("tool_id_and_name_mapping") or {})
    tool_id_and_name_mapping[EXECUTE_PLAN_TOOL_NAME] = {"type": "CODE_EXEC"}
    params["tool_id_and_name_mapping"] = tool_id_and_name_mapping

    if reference_block:
        configuration["prompt"] = f"{configuration.get('prompt', '')}\n\n{reference_block}"
