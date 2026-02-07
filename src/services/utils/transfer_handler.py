from typing import Any


def check_transfer_from_codes_mapping(
    codes_mapping: dict[str, Any], tool_id_and_name_mapping: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """
    Check if there's a transfer action in the codes_mapping and create transfer config

    Args:
        codes_mapping: Dictionary containing tool call mappings with args

    Returns:
        Tuple of (has_transfer: bool, transfer_config: dict)
    """
    for tool_call_id, tool_data in codes_mapping.items():
        if tool_data.get("error"):
            continue

        args = tool_data.get("args", {})
        if not isinstance(args, dict):
            continue
        action_type = args.get("action_type")

        if action_type == "transfer":
            tool_name = tool_data.get("name", "")

            if tool_name in tool_id_and_name_mapping:
                agent_id = tool_id_and_name_mapping[tool_name].get(
                    "agent_id", tool_id_and_name_mapping[tool_name].get("bridge_id", "")
                )

            transfer_config = {
                "agent_id": agent_id,
                "tool_name": tool_name,
                "user_query": args.get("_query", ""),
                "action_type": action_type,
                "all_arguments": args,
                "tool_call_id": tool_call_id,
            }

            return True, transfer_config

    return False, {}
