"""Bridge between AI-generated code and the existing tool execution
primitives.

Reuses the exact per-type dispatch (`build_single_tool_task`) and
gateway-variable injection (`apply_variables_path`) that the normal
tool-calling loop already uses in
`src.services.commonServices.baseService.utils.process_data_and_run_tools`
/ `BaseService.replace_variables_in_args` — no separate reimplementation
of that logic here, so there's exactly one place that decides how a tool
call is dispatched, not two that can drift apart.
"""

from src.services.commonServices.baseService.utils import apply_variables_path, build_single_tool_task


class UnknownTool(Exception):
    """The generated script referenced a tool name that isn't configured for this bridge."""


async def execute_single_tool(name, args, ctx):
    """Dispatch one tool call through the same primitives/variable-injection
    the normal tool-calling loop uses, one call at a time (no gather/batching
    needed here — the generated script already sequences its own calls).

    ctx: dict with tool_id_and_name_mapping, org_id, owner_id, message_id,
    thread_id, sub_thread_id, bridge_configurations, timer, stream_mode,
    streamer, variables, variables_path.

    Returns (response_content, resolved_args). Raises on tool failure so the
    generated code's own try/except (or an uncaught propagation, which
    aborts the rest of the script) can react exactly like a failed step
    would in a real dependency chain.
    """
    tool_id_and_name_mapping = ctx.get("tool_id_and_name_mapping") or {}
    tool_mapping = tool_id_and_name_mapping.get(name)
    if not tool_mapping:
        raise UnknownTool(f"Unknown tool: {name!r}. Available tools: {sorted(tool_id_and_name_mapping)}")

    resolved_args = dict(args or {})
    function_name = (
        tool_mapping.get("bridge_id")
        if tool_mapping.get("type") == "AGENT"
        else tool_mapping.get("name", name)
    )
    apply_variables_path(resolved_args, function_name, ctx.get("variables"), ctx.get("variables_path"))

    result = await build_single_tool_task(name, resolved_args, tool_mapping, ctx)

    if not isinstance(result, dict) or result.get("status") != 1:
        error_detail = result.get("response") if isinstance(result, dict) else str(result)
        raise RuntimeError(f"Tool '{name}' failed: {error_detail}")

    return result.get("response"), resolved_args


def make_call_tool(ctx, invocation_log):
    """Build the single async hook exposed to generated code. Every
    successful invocation (name, resolved args, response) is appended to
    invocation_log so the caller can persist it into tools_call_data for
    future structured context / historical examples.

    No call-count limit here — each individual tool call already has its
    own timeout at the dispatch level.
    """

    async def call_tool(name, args=None):
        response, resolved_args = await execute_single_tool(name, args or {}, ctx)
        invocation_log.append({"name": name, "args": resolved_args, "response": response})
        return response

    return call_tool
