import contextvars
import json
import re
import subprocess
from typing import Any

import aiohttp
from langchain_core.tools import StructuredTool, tool

from db.agent_db_service import get_agent
from db.tool_db_service import get_tools_by_ids

SOKT_BASE_URL = "https://flow.sokt.io/func"

# Context variable to pass runtime variables to tool functions at call time
runtime_variables_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "runtime_variables", default={}
)


def _resolve_path(data: dict, path: str) -> Any:
    """Resolve a dot-separated path like 'variables.orgId' from a nested dict."""
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _resolve_template(value: Any, runtime_vars: dict) -> Any:
    """Resolve {{path}} templates in a value using runtime variables.

    Examples:
        '{{variables.orgId}}'  -> '16053'
        'prefix_{{variables.env}}_suffix' -> 'prefix_prod_suffix'
        42 (non-string) -> 42 (returned as-is)
    """
    if not isinstance(value, str) or runtime_vars is None:
        return value

    # Full-match: entire value is a single {{path}} — return resolved value with original type
    full_match = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", value)
    if full_match:
        resolved = _resolve_path(runtime_vars, full_match.group(1))
        return resolved if resolved is not None else value

    # Partial-match: replace all {{path}} occurrences within the string
    def _replacer(m):
        resolved = _resolve_path(runtime_vars, m.group(1).strip())
        return str(resolved) if resolved is not None else m.group(0)

    return re.sub(r"\{\{\s*(.+?)\s*\}\}", _replacer, value)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _make_function_name(name: str) -> str:
    """Strip characters not allowed in LLM tool names (mirrors makeFunctionName)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


def _get_tool_schema_dict(tool_fn: Any) -> dict:
    """Return JSON-like args schema for a tool."""
    args_schema = getattr(tool_fn, "args_schema", None)
    if not args_schema:
        return {}
    try:
        if hasattr(args_schema, "model_json_schema"):  # pydantic v2
            return args_schema.model_json_schema() or {}
        if hasattr(args_schema, "schema"):  # pydantic v1
            return args_schema.schema() or {}
    except Exception:
        return {}
    return {}


def build_tool_payload_hint(tool_fn: Any) -> str:
    """Return readable guidance of exact accepted payload keys."""
    schema = _get_tool_schema_dict(tool_fn)
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not properties:
        return "No explicit args schema."
    keys = []
    for k, meta in properties.items():
        keys.append(f"{k} ({meta.get('type', 'string')}, {'required' if k in required else 'optional'})")
    return "Use exact keys: " + ", ".join(keys)


def normalize_tool_payload(tool_fn: Any, raw_args: Any) -> dict:
    """Normalize model-produced args to exact tool schema keys."""
    payload = raw_args if isinstance(raw_args, dict) else {}
    if not isinstance(payload, dict):
        return {}

    schema = _get_tool_schema_dict(tool_fn)
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    if not properties:
        return payload

    # Unwrap common nesting wrappers.
    for wrapper in ("args", "payload", "data"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            payload = nested
            break

    # Exact keys first.
    normalized = {k: v for k, v in payload.items() if k in properties}

    # If nothing matched, and schema has single key, map known aliases to it.
    if not normalized and len(properties) == 1:
        only = next(iter(properties.keys()))
        for alias in ("input", "task", "query", "text", "message", "prompt"):
            if alias in payload:
                normalized[only] = payload.get(alias)
                break

    # If one required key missing and there is one unknown value, map it.
    missing_required = [k for k in required if k not in normalized]
    unknown_items = [(k, v) for k, v in payload.items() if k not in properties]
    if len(missing_required) == 1 and len(unknown_items) == 1:
        normalized[missing_required[0]] = unknown_items[0][1]

    return normalized


async def _axios_work(args: dict, url: str, headers: dict = None) -> dict:
    """POST args as JSON body to url. Mirrors axios_work() from the real project."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=args,
                headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 300:
                    error_text = await resp.text()
                    return {"response": error_text, "status": 0}
                response_data = await resp.json(content_type=None)
                return {"response": response_data, "status": 1}
    except Exception as err:
        return {"response": str(err), "status": 0}

# ──────────────────────────────────────────────
# api_call tool builder  (mirrors process_api_call_tool + axios_work)
# ──────────────────────────────────────────────

def _build_api_call_tool(tool_config: dict) -> StructuredTool:
    """Build a LangChain StructuredTool from an api_call DB record.

    Fields with source='user' are injected silently from static_values at call time.
    Only fields with source='ai' (or no source) are exposed in the LLM schema.
    """
    from pydantic import Field as PField, create_model

    script_id = tool_config.get("script_id", "")
    tool_name = _make_function_name(tool_config.get("title") or tool_config["name"])
    tool_description = tool_config.get("description", f"Calls the {tool_name} function")
    url = f"{SOKT_BASE_URL}/{script_id}"
    headers = tool_config.get("headers", {})

    all_fields: dict = tool_config.get("fields", {})
    required_params: list = tool_config.get("required_params", [])
    static_values: dict = tool_config.get("static_values", {})

    # Split fields: 'user' source fields are injected silently; 'ai' fields go to LLM
    ai_fields = {
        k: v for k, v in all_fields.items()
        if v.get("source", "ai") != "user"
    }
    # Collect user-source field names to inject from static_values at call time
    user_field_names = {
        k for k, v in all_fields.items()
        if v.get("source") == "user"
    }

    # Only required_params that are AI-driven stay in the LLM schema
    ai_required = [p for p in required_params if p not in user_field_names]

    # Build the dynamic async function — merges AI kwargs + user static values + runtime variable resolution
    async def _call(**kwargs) -> str:
        runtime_vars = runtime_variables_ctx.get()
        merged = dict(kwargs)
        for k, v in static_values.items():
            if k not in merged:
                resolved = _resolve_template(v, runtime_vars)
                # Skip unresolved {{path}} templates so they don't hit the API
                if isinstance(resolved, str) and re.search(r"\{\{.+?\}\}", resolved):
                    continue
                merged[k] = resolved
        result = await _axios_work(merged, url, headers)
        if result["status"] == 1:
            resp = result["response"]
            return json.dumps(resp) if not isinstance(resp, str) else resp
        return f"Tool error: {result['response']}"

    # Build LLM-visible schema from ai_fields only
    properties = {
        k: {
            "type": v.get("type", "string"),
            "description": f"{v.get('description', k)} (Exact payload key: '{k}')",
        }
        for k, v in ai_fields.items()
    }

    # If no AI fields defined, use a single generic 'input' param
    if not properties:
        properties = {"input": {"type": "string", "description": "Input for the tool"}}
        ai_required = ["input"]

    field_definitions = {}
    for param_name, param_meta in properties.items():
        python_type = str
        if param_meta.get("type") == "integer":
            python_type = int
        elif param_meta.get("type") == "number":
            python_type = float
        elif param_meta.get("type") == "boolean":
            python_type = bool

        if param_name in ai_required:
            field_definitions[param_name] = (python_type, PField(..., description=param_meta.get("description", param_name)))
        else:
            field_definitions[param_name] = (python_type, PField(None, description=param_meta.get("description", param_name)))

    DynamicSchema = create_model(f"{tool_name}_schema", **field_definitions)

    return StructuredTool.from_function(
        coroutine=_call,
        name=tool_name,
        description=tool_description,
        args_schema=DynamicSchema,
    )


# ──────────────────────────────────────────────
# function tool builder (built-in lookup)
# ──────────────────────────────────────────────

def _build_function_tool(tool_config: dict) -> Any:
    """Return a built-in tool by function_name, or a no-op placeholder."""
    function_name = tool_config.get("function_name") or tool_config["name"]

    async def noop_fn(input: str) -> str:
        return f"Function '{function_name}' is not available."

    return StructuredTool.from_function(
        coroutine=noop_fn,
        name=_make_function_name(tool_config["name"]),
        description=tool_config.get("description", f"Function: {function_name}"),
    )


# ──────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────

def build_langchain_tool(tool_config: dict) -> Any:
    """Convert a DB tool record into a LangChain tool."""
    tool_type = tool_config.get("type", "api_call")

    if tool_type == "api_call":
        return _build_api_call_tool(tool_config)
    elif tool_type == "function":
        return _build_function_tool(tool_config)
    else:
        async def placeholder_fn(input: str) -> str:
            return f"Tool type '{tool_type}' is not yet supported."

        return StructuredTool.from_function(
            coroutine=placeholder_fn,
            name=_make_function_name(tool_config["name"]),
            description=tool_config.get("description", "Unsupported tool"),
        )


def extract_tool_schemas(tools: list) -> list[dict]:
    """Extract lightweight schema dicts from LangChain tools for planner visibility.

    Returns a list of {name, description, parameters} where parameters lists
    each param's name, type, required status, and description.
    """
    schemas = []
    for t in tools:
        params = []
        try:
            schema = t.args_schema.schema() if t.args_schema else {}
            properties = schema.get("properties", {})
            required_fields = schema.get("required", [])
            for pname, pmeta in properties.items():
                params.append({
                    "name": pname,
                    "type": pmeta.get("type", "string"),
                    "required": pname in required_fields,
                    "description": pmeta.get("description", ""),
                })
        except Exception:
            pass

        schemas.append({
            "name": t.name,
            "description": t.description or "",
            "parameters": params,
        })
    return schemas


async def execute_pretool(tool_id: str, pretool_input: dict = None) -> str:
    """Execute a single tool by its ID with the given input and return its output string.

    Used to run the agent's 'pretool' before the AI API call.
    The output replaces {{pretool}} placeholders in the system prompt.
    """
    from db.tool_db_service import get_tool

    tool_config = await get_tool(tool_id)
    if not tool_config:
        return f"[pretool error: tool '{tool_id}' not found]"

    if tool_config.get("status") != "active":
        return f"[pretool error: tool '{tool_id}' is not active]"

    try:
        lc_tool = build_langchain_tool(tool_config)
        result = await lc_tool.ainvoke(pretool_input or {})
        return str(result) if not isinstance(result, str) else result
    except Exception as e:
        return f"[pretool error: {e}]"


async def load_tools_for_agent(agent_id: str) -> list:
    """Load all active tools for an agent from the DB and convert to LangChain tools."""
    agent = await get_agent(agent_id)
    tool_ids = agent.get("tools", [])

    db_tools = await get_tools_by_ids(tool_ids)
    langchain_tools = []

    for db_tool in db_tools:
        try:
            lc_tool = build_langchain_tool(db_tool)
            langchain_tools.append(lc_tool)
        except Exception:
            pass

    return langchain_tools
