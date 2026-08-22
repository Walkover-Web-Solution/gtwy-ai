"""Entry point invoked from process_data_and_run_tools when the AI calls the
execute_plan meta-tool (mode == "auto_tools" only — see prompt_builder.py
for where that tool gets registered). Validates and runs the generated code, using the exact tool-dispatch
primitives already used by the normal tool-calling loop.
"""

from globals import logger
from src.services.auto_exec.code_runner import CodeRejected, run_generated_code
from src.services.auto_exec.tool_bridge import make_call_tool


def _strip_code_fences(code: str) -> str:
    raw = code.strip()
    if "```python" in raw:
        return raw.split("```python", 1)[1].split("```", 1)[0].strip()
    if "```" in raw:
        return raw.split("```", 1)[1].split("```", 1)[0].strip()
    return raw


async def run_generated_plan(args, service_instance):
    """Called as one branch of process_data_and_run_tools' per-tool
    dispatch (tool_id_and_name_mapping[name]["type"] == "CODE_EXEC").
    `service_instance` is the BaseService instance already running the
    normal loop — reused for its org_id/thread_id/variables/etc, exactly
    like the AGENT/RAG/MCP branches reuse it.

    Returns the same {"response", "metadata", "status"} envelope shape as
    axios_work/call_gtwy_agent/call_mcp_tool so the caller's existing
    result handling needs no special-casing.
    """
    code = (args or {}).get("code")
    if not isinstance(code, str) or not code.strip():
        return {"response": "execute_plan requires a non-empty 'code' string.", "status": 0}

    code = _strip_code_fences(code)

    tool_id_and_name_mapping = {
        k: v
        for k, v in (service_instance.tool_id_and_name_mapping or {}).items()
        if v.get("type") != "CODE_EXEC"  # a plan can't call execute_plan itself
    }

    ctx = {
        "tool_id_and_name_mapping": tool_id_and_name_mapping,
        "org_id": service_instance.org_id,
        "owner_id": service_instance.owner_id,
        "message_id": service_instance.message_id,
        "thread_id": service_instance.thread_id,
        "sub_thread_id": service_instance.sub_thread_id,
        "bridge_configurations": service_instance.bridge_configurations,
        "timer": getattr(service_instance, "timer", None),
        "stream_mode": service_instance.stream_mode,
        "streamer": service_instance.streamer,
        "variables": service_instance.variables,
        "variables_path": service_instance.variables_path,
    }

    invocation_log = []
    # No call-count limit — see tool_bridge.make_call_tool. Each individual
    # tool call already has its own timeout at the dispatch level.
    call_tool = make_call_tool(ctx, invocation_log)

    try:
        result_value = await run_generated_code(code, call_tool)
        return {
            "response": result_value,
            "metadata": {"type": "code_exec", "sub_calls": invocation_log},
            "status": 1,
        }
    except CodeRejected as exc:
        logger.warning(f"auto_exec plan rejected/failed: {exc}")
        return {
            "response": f"Plan execution failed: {exc}",
            "metadata": {"type": "code_exec", "sub_calls": invocation_log},
            "status": 0,
        }
    except Exception as exc:
        # Includes uncaught tool failures (RuntimeError from execute_single_tool)
        # propagating out of the generated code — the script simply stops at
        # that line, so anything after it never ran, mirroring "failed step
        # blocks its dependents" with no extra bookkeeping.
        logger.error(f"auto_exec plan execution error: {exc}")
        return {
            "response": f"Plan execution error: {exc}",
            "metadata": {"type": "code_exec", "sub_calls": invocation_log},
            "status": 0,
        }
