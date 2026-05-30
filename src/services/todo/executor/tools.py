from globals import logger


def filter_tools_for_task(agent_config: dict, task_tool_names) -> list:
    """Return the subset of the agent's tools whose name matches task_tool_names.

    Accepts a single string or a list. Falls back to case-insensitive matching
    when exact match fails so a capitalisation mismatch doesn't block the worker.
    """
    all_tools = ((agent_config or {}).get("configuration") or {}).get("tools") or []

    if task_tool_names is None or task_tool_names == "":
        return []

    allow = {task_tool_names} if isinstance(task_tool_names, str) else set(task_tool_names)

    def _name(tool):
        return tool.get("name") or (tool.get("function") or {}).get("name")

    exact = [t for t in all_tools if _name(t) in allow]
    if exact:
        return exact

    allow_lower = {a.lower() for a in allow if isinstance(a, str)}
    ci = [t for t in all_tools if (_name(t) or "").lower() in allow_lower]
    if ci:
        logger.warning(
            f"Tool filter matched case-insensitively: requested={sorted(allow)} "
            f"matched={[_name(t) for t in ci]}"
        )
        return ci

    logger.error(
        f"Tool filter found NO match. requested={sorted(allow)} "
        f"available={[_name(t) for t in all_tools if _name(t)]}"
    )
    return []


def inject_variables_into_tool_args(
    tool_name: str,
    args: dict,
    variables: dict,
    variables_path: dict,
    tool_id_and_name_mapping: dict,
) -> dict:
    """Inject static bridge variables into tool args based on variables_path mapping."""
    if not variables_path or not variables:
        return args

    import pydash as _

    tool_mapping = tool_id_and_name_mapping.get(tool_name, {})
    function_name = (
        tool_mapping.get("bridge_id", "")
        if tool_mapping.get("type") == "AGENT"
        else tool_mapping.get("name", tool_name)
    )

    enriched_args = dict(args or {})
    for path_key, path_value in variables_path.get(function_name, {}).items():
        value = _.objects.get(variables, path_value)
        if value is not None:
            _.objects.set_(enriched_args, path_key, value)

    return enriched_args
