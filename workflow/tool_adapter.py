import json
import re
from typing import Any, Literal, Optional, Protocol

import pydash as _

from langchain_core.tools import StructuredTool
from pydantic import Field as PydanticField, create_model

from src.configs.constant import inbuild_tools
from src.controllers.rag_controller import get_text_from_vectorsQuery
from src.services.commonServices.baseService.utils import axios_work
from src.services.utils.ai_call_util import call_gtwy_agent
from src.services.utils.built_in_tools.firecrawl import call_firecrawl_scrape


# ---------------------------------------------------------------------------
# Strategy pattern for tool type handlers
# ---------------------------------------------------------------------------

class ToolTypeHandler(Protocol):
    async def execute(self, tool_args: dict, tool_mapping: dict, executor: "WorkflowToolExecutor") -> Any: ...


class RAGHandler:
    async def execute(self, tool_args: dict, tool_mapping: dict, executor: "WorkflowToolExecutor") -> Any:
        resource_to_collection_mapping = tool_mapping.get("resource_to_collection_mapping", {})
        return await get_text_from_vectorsQuery(
            {**tool_args, "org_id": executor.org_id},
            Flag=True,
            owner_id=executor.owner_id,
            resource_to_collection_mapping=resource_to_collection_mapping,
        )


class AgentHandler:
    async def execute(self, tool_args: dict, tool_mapping: dict, executor: "WorkflowToolExecutor") -> Any:
        agent_args = {
            "org_id": executor.org_id,
            "bridge_id": tool_mapping.get("bridge_id"),
            "user": tool_args.get("_query"),
            "variables": {key: value for key, value in tool_args.items() if key not in {"_query", "action_type"}},
            "message_id": executor.message_id,
            "bridge_configurations": executor.bridge_configurations,
        }
        if tool_mapping.get("requires_thread_id", False):
            agent_args["thread_id"] = executor.thread_id
            agent_args["sub_thread_id"] = executor.sub_thread_id
        if tool_mapping.get("version_id"):
            agent_args["version_id"] = tool_mapping.get("version_id")
        return await call_gtwy_agent(agent_args)


class FirecrawlHandler:
    async def execute(self, tool_args: dict, tool_mapping: dict, executor: "WorkflowToolExecutor") -> Any:
        return await call_firecrawl_scrape(tool_args)


class HTTPHandler:
    async def execute(self, tool_args: dict, tool_mapping: dict, executor: "WorkflowToolExecutor") -> Any:
        return await axios_work(tool_args, tool_mapping)


def _normalize_type(field_meta: dict) -> tuple[Any, Any]:
    meta = field_meta or {}
    enum_values = meta.get("enum")
    if enum_values and isinstance(enum_values, list) and len(enum_values) > 0:
        return Literal[tuple(enum_values)], None
    field_type = meta.get("type", "string")
    if field_type == "integer":
        return int, None
    if field_type == "number":
        return float, None
    if field_type == "boolean":
        return bool, None
    if field_type == "array":
        return list, None
    if field_type == "object":
        return dict, None
    return str, None


def _to_safe_field_name(param_name: str) -> str:
    safe_name = re.sub(r"\W", "_", str(param_name or "input"))
    safe_name = safe_name.lstrip("_")
    if not safe_name:
        safe_name = "input"
    if safe_name[0].isdigit():
        safe_name = f"field_{safe_name}"
    return safe_name


class WorkflowToolExecutor:
    _default_handler = HTTPHandler()
    _handlers: dict[str, ToolTypeHandler] = {
        "RAG": RAGHandler(),
        "AGENT": AgentHandler(),
        inbuild_tools["Gtwy_Web_Search"]: FirecrawlHandler(),
    }

    def __init__(self, parsed_data: dict, bridge_configurations: dict):
        self.parsed_data = parsed_data
        self.bridge_configurations = bridge_configurations or {}
        self.variables = parsed_data.get("variables") or {}
        self.variables_path = parsed_data.get("variables_path") or {}
        self.tool_id_and_name_mapping = parsed_data.get("tool_id_and_name_mapping") or {}
        self.org_id = parsed_data.get("org_id")
        self.owner_id = parsed_data.get("owner_id")
        self.thread_id = parsed_data.get("thread_id")
        self.sub_thread_id = parsed_data.get("sub_thread_id")
        self.message_id = parsed_data.get("message_id")

    def inject_runtime_values(self, tool_name: str, args: dict) -> dict:
        merged = dict(args or {})
        tool_mapping = self.tool_id_and_name_mapping.get(tool_name, {})
        if tool_mapping.get("type") == "AGENT":
            function_name = tool_mapping.get("bridge_id", "")
        else:
            function_name = tool_mapping.get("name", tool_name)

        for path_key, path_value in (self.variables_path.get(function_name) or {}).items():
            resolved = _.objects.get(self, path_value)
            if resolved is not None:
                _.objects.set_(merged, path_key, resolved)
        return merged

    async def execute(self, tool_name: str, args: dict) -> str:
        tool_mapping = self.tool_id_and_name_mapping.get(tool_name)
        if not tool_mapping:
            return f"Unknown tool: {tool_name}"

        tool_args = self.inject_runtime_values(tool_name, args)
        tool_type = tool_mapping.get("type")

        try:
            handler = self._handlers.get(tool_type, self._default_handler)
            result = await handler.execute(tool_args, tool_mapping, self)

            if isinstance(result, dict) and result.get("status") == 1:
                response = result.get("response", "")
                return response if isinstance(response, str) else json.dumps(response)
            if isinstance(result, dict):
                response = result.get("response", "Unknown tool error")
                return f"Tool error: {response if isinstance(response, str) else json.dumps(response)}"
            return result if isinstance(result, str) else json.dumps(result)
        except Exception as error:
            return f"Tool error: {error}"


def extract_tool_schemas(tool_defs: list[dict]) -> list[dict]:
    tool_schemas = []
    for tool_def in tool_defs or []:
        if tool_def.get("type") != "function":
            continue
        properties = tool_def.get("properties") or {}
        required = set(tool_def.get("required") or [])
        params = []
        for param_name, param_meta in properties.items():
            params.append(
                {
                    "name": param_name,
                    "type": (param_meta or {}).get("type", "string"),
                    "description": (param_meta or {}).get("description", ""),
                    "required": param_name in required,
                }
            )
        tool_schemas.append(
            {
                "name": tool_def.get("name"),
                "description": tool_def.get("description", ""),
                "parameters": params,
            }
        )
    return tool_schemas


def normalize_tool_payload(tool_fn: Any, raw_args: Any) -> dict:
    payload = raw_args if isinstance(raw_args, dict) else {}
    args_schema = getattr(tool_fn, "args_schema", None)
    if not args_schema or not hasattr(args_schema, "model_json_schema"):
        return payload

    schema = args_schema.model_json_schema() or {}
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    if not properties:
        return payload

    for wrapper in ("args", "payload", "data"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            payload = nested
            break

    normalized = {key: value for key, value in payload.items() if key in properties}
    if not normalized and len(properties) == 1:
        only = next(iter(properties.keys()))
        for alias in ("input", "task", "query", "text", "message", "prompt"):
            if alias in payload:
                normalized[only] = payload.get(alias)
                break

    missing_required = [key for key in required if key not in normalized]
    unknown_items = [(key, value) for key, value in payload.items() if key not in properties]
    if len(missing_required) == 1 and len(unknown_items) == 1:
        normalized[missing_required[0]] = unknown_items[0][1]
    return normalized


def build_tool_payload_hint(tool_fn: Any) -> str:
    args_schema = getattr(tool_fn, "args_schema", None)
    if not args_schema or not hasattr(args_schema, "model_json_schema"):
        return "No explicit args schema."
    schema = args_schema.model_json_schema() or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = []
    for key, meta in properties.items():
        parts.append(f"{key} ({meta.get('type', 'string')}, {'required' if key in required else 'optional'})")
    return "Use exact keys: " + ", ".join(parts) if parts else "No explicit args schema."


def build_langchain_tools(parsed_data: dict, bridge_configurations: dict) -> tuple[list[StructuredTool], list[dict]]:
    tool_defs = parsed_data.get("tools") or []
    executor = WorkflowToolExecutor(parsed_data, bridge_configurations)
    tools: list[StructuredTool] = []

    for tool_def in tool_defs:
        if tool_def.get("type") != "function" or not tool_def.get("name"):
            continue

        properties = tool_def.get("properties") or {}
        required = set(tool_def.get("required") or [])
        field_definitions = {}
        field_aliases = {}
        for param_name, param_meta in properties.items():
            safe_name = _to_safe_field_name(param_name)
            field_type, default = _normalize_type(param_meta or {})
            field_aliases[safe_name] = param_name
            if param_name in required:
                field_definitions[safe_name] = (
                    field_type,
                    PydanticField(
                        ...,
                        alias=param_name,
                        description=(param_meta or {}).get("description", ""),
                    ),
                )
            else:
                field_definitions[safe_name] = (
                    Optional[field_type],
                    PydanticField(
                        default,
                        alias=param_name,
                        description=(param_meta or {}).get("description", ""),
                    ),
                )

        if not field_definitions:
            field_definitions["input"] = (
                Optional[str],
                PydanticField(None, description="Optional free-form tool input."),
            )

        args_schema = create_model(f"WorkflowToolArgs_{tool_def['name']}", **field_definitions)

        async def _tool_runner(
            _tool_name: str = tool_def["name"],
            _field_aliases: dict[str, str] = field_aliases,
            **kwargs,
        ) -> str:
            normalized_kwargs = {_field_aliases.get(key, key): value for key, value in kwargs.items()}
            return await executor.execute(_tool_name, normalized_kwargs)

        tools.append(
            StructuredTool.from_function(
                coroutine=_tool_runner,
                name=tool_def["name"],
                description=tool_def.get("description", ""),
                args_schema=args_schema,
            )
        )

    return tools, extract_tool_schemas(tool_defs)
