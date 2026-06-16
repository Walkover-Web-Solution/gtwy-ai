import json

from globals import logger
from src.configs.constant import service_name


MCP_NAME_SUFFIX = "__mcp"
_MCP_TOOL_MAP_KEY = "__mcp_tool_map__"


def _parse_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _anthropic_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                parts.append(json.dumps(item))
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(str(item))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def extract_server_side_mcp_calls(service: str, model_response: dict) -> dict:
    """
    Pull MCP tool invocations performed by the LLM provider itself (Strategy A)
    out of the raw model response and return a {call_id: entry} dict shaped
    exactly like the client-side tool_call_logs entries.
    """
    if not isinstance(model_response, dict):
        return {}

    entries: dict = {}

    if service in (
        service_name["openai"],
        service_name["groq"],
        service_name["grok"],
        service_name["mistral"],
    ):
        for item in model_response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "mcp_call":
                continue
            call_id = item.get("id") or f"mcp_{len(entries)}"
            server = item.get("server_label")
            tool_name = item.get("name")
            error = item.get("error")
            entries[call_id] = {
                "name": tool_name,
                "id": tool_name,
                "type": "MCP",
                "mcp_server": server,
                "mcp_tool": tool_name,
                "source": "server_side",
                "args": _parse_arguments(item.get("arguments")),
                "data": {
                    "response": item.get("output"),
                    "metadata": {
                        "type": "mcp",
                        "server": server,
                        "tool": tool_name,
                        "source": "server_side",
                    },
                    "status": 0 if error else 1,
                    "error": error,
                },
            }
        return entries

    if service == service_name["anthropic"]:
        content = model_response.get("content") or []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "mcp_tool_use":
                tool_use_id = block.get("id") or f"mcp_{len(entries)}"
                tool_name = block.get("name")
                server = block.get("server_name")
                entries[tool_use_id] = {
                    "name": tool_name,
                    "id": tool_name,
                    "type": "MCP",
                    "mcp_server": server,
                    "mcp_tool": tool_name,
                    "source": "server_side",
                    "args": block.get("input") or {},
                    "data": {
                        "response": None,
                        "metadata": {
                            "type": "mcp",
                            "server": server,
                            "tool": tool_name,
                            "source": "server_side",
                        },
                        "status": 1,
                    },
                }
            elif btype == "mcp_tool_result":
                ref = block.get("tool_use_id")
                target = entries.get(ref)
                if not target:
                    continue
                is_error = bool(block.get("is_error"))
                target["data"]["response"] = _anthropic_result_text(block.get("content"))
                target["data"]["status"] = 0 if is_error else 1
                if is_error:
                    target["data"]["error"] = True
        return entries

    return {}


def server_side_mcp_tools_data(service: str, model_response: dict) -> dict:
    """
    Flatten server-side MCP calls in ``model_response`` into the
    ``{tool_name: response_content}`` shape used by ``tools_data`` in the
    completion response (matches what update_configration writes for
    client-side calls).
    """
    flat: dict = {}
    for entry in extract_server_side_mcp_calls(service, model_response).values():
        name = entry.get("mcp_tool") or entry.get("name")
        if not name:
            continue
        flat[name] = (entry.get("data") or {}).get("response")
    return flat


def merge_server_side_mcp_into_tools(service: str, model_response: dict, tools) -> dict:
    """
    Return a new dict with any server-side MCP entries layered on top of the
    given tools_data. No-op when the response carries no server-side calls.
    """
    extras = server_side_mcp_tools_data(service, model_response)
    if not extras:
        return tools or {}
    merged = dict(tools or {})
    merged.update(extras)
    return merged


def resolve_mcp_type(service: str, model: str) -> str:
    from src.configs.model_configuration import model_config_document
    try:
        mcp_type = (
            model_config_document.get(service, {})
            .get(model, {})
            .get("validationConfig", {})
            .get("mcp_type")
        )
    except Exception as e:
        logger.error(f"resolve_mcp_type: lookup failed for {service}/{model}: {e}")
        return "client"
    return mcp_type if mcp_type in ("server", "client") else "client"


def _bearer_token_from_headers(headers: dict) -> str:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization" and isinstance(value, str):
            stripped = value.strip()
            if stripped.lower().startswith("bearer "):
                return stripped[7:].strip()
            return stripped
    return ""


def build_mcp_tool_name(tool_name: str, server_name: str, multi_server: bool) -> str:
    if multi_server:
        return f"{tool_name}__{server_name}{MCP_NAME_SUFFIX}"
    return f"{tool_name}{MCP_NAME_SUFFIX}"


def display_mcp_tool_name(tool_name: str) -> str:
    if not isinstance(tool_name, str) or not tool_name.endswith(MCP_NAME_SUFFIX):
        return tool_name
    without_suffix = tool_name[: -len(MCP_NAME_SUFFIX)]
    if "__" in without_suffix:
        return without_suffix.rsplit("__", 1)[0]
    return without_suffix


def client_mcp_config(
    service: str,
    configuration: dict,
    mcp_config: dict,
    tool_id_and_name_mapping: dict | None = None,
) -> None:
    servers = mcp_config.get("servers") or []
    if not servers:
        return

    multi_server = len(servers) > 1
    configuration.setdefault("tools", [])
    tool_map = configuration.setdefault(_MCP_TOOL_MAP_KEY, {})

    for server in servers:
        if not isinstance(server, dict):
            continue
        server_name = server.get("name")
        if not server_name:
            continue
        server_url = server.get("url")
        server_headers = server.get("headers") or {}
        for tool in server.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            original_name = tool.get("name")
            if not original_name:
                continue
            input_schema = tool.get("inputSchema") or {}
            exposed_name = build_mcp_tool_name(original_name, server_name, multi_server)
            configuration["tools"].append(
                {
                    "name": exposed_name,
                    "description": tool.get("description", ""),
                    "properties": input_schema.get("properties", {}) or {},
                    "required": input_schema.get("required", []) or [],
                }
            )
            tool_map[exposed_name] = {"server": server_name, "tool": original_name}
            if isinstance(tool_id_and_name_mapping, dict):
                tool_id_and_name_mapping[exposed_name] = {
                    "type": "MCP",
                    "name": exposed_name,
                    "mcp_server": server_name,
                    "mcp_tool": original_name,
                    "mcp_url": server_url,
                    "mcp_headers": server_headers,
                }


def server_mcp_config(service: str, new_config: dict, mcp_config: dict) -> None:
    servers = mcp_config.get("servers") or []
    if not servers:
        return

    if service in (
        service_name["openai"],
        service_name["groq"],
        service_name["grok"],
        service_name["mistral"],
    ):
        new_config.setdefault("tools", [])
        for server in servers:
            if not isinstance(server, dict) or not server.get("url"):
                continue
            entry = {
                "type": "mcp",
                "server_label": server.get("name", ""),
                "server_url": server["url"],
                "require_approval": "never"
            }
            description = server.get("description")
            if description:
                entry["server_description"] = description
            headers = server.get("headers") or {}
            if headers:
                entry["headers"] = headers
            new_config["tools"].append(entry)
        return

    if service == service_name["anthropic"]:
        mcp_servers = []
        for server in servers:
            if not isinstance(server, dict) or not server.get("url"):
                continue
            server_entry = {
                "type": "url",
                "name": server.get("name", ""),
                "url": server["url"],
            }
            token = _bearer_token_from_headers(server.get("headers") or {})
            if token:
                server_entry["authorization_token"] = token
            mcp_servers.append(server_entry)

        if not mcp_servers:
            return

        new_config["mcp_servers"] = mcp_servers

        betas = list(new_config.get("betas") or [])
        if "mcp-client-2025-04-04" not in betas:
            betas.append("mcp-client-2025-04-04")
        new_config["betas"] = betas
        return

    logger.warning(
        f"server_mcp_config: service '{service}' is marked mcp_type='server' "
        "but no Strategy A serializer is wired for it; skipping."
    )
