import json
import re
import subprocess
from typing import Any

import aiohttp
from langchain_core.tools import StructuredTool, tool

from db.agent_db_service import get_agent
from db.tool_db_service import get_tools_by_ids

SOKT_BASE_URL = "https://flow.sokt.io/func"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _make_function_name(name: str) -> str:
    """Strip characters not allowed in LLM tool names (mirrors makeFunctionName)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


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
# Built-in tools
# ──────────────────────────────────────────────

@tool
def read_file(path: str) -> str:
    """Read the contents of a file at the given path."""
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file at the given path."""
    try:
        with open(path, "w") as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool
def run_shell(command: str) -> str:
    """Run a shell command and return its output."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout or result.stderr
        return output.strip() or "Command ran with no output."
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running command: {e}"


@tool
def list_files(directory: str = ".") -> str:
    """List files and directories at the given path."""
    try:
        result = subprocess.run(
            f"ls -la {directory}", shell=True, capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error listing files: {e}"


BUILTIN_TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
    "list_files": list_files,
}


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

    # Build the dynamic async function — merges AI kwargs + user static values
    async def _call(**kwargs) -> str:
        merged = dict(kwargs)
        for field_name in user_field_names:
            if field_name in static_values:
                merged[field_name] = static_values[field_name]
        result = await _axios_work(merged, url, headers)
        if result["status"] == 1:
            resp = result["response"]
            return json.dumps(resp) if not isinstance(resp, str) else resp
        return f"Tool error: {result['response']}"

    # Build LLM-visible schema from ai_fields only
    properties = {
        k: {
            "type": v.get("type", "string"),
            "description": v.get("description", k),
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

    if function_name in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[function_name]

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


async def load_tools_for_agent(agent_id: str) -> list:
    """Load all active tools for an agent from the DB and convert to LangChain tools."""
    agent = await get_agent(agent_id)
    if not agent:
        return list(BUILTIN_TOOLS.values())

    tool_ids = agent.get("tools", [])
    if not tool_ids:
        return list(BUILTIN_TOOLS.values())

    db_tools = await get_tools_by_ids(tool_ids)
    langchain_tools = []

    for db_tool in db_tools:
        try:
            lc_tool = build_langchain_tool(db_tool)
            langchain_tools.append(lc_tool)
        except Exception:
            pass

    return langchain_tools
