from typing import Optional

from pydantic import BaseModel, Field


class CreateToolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Tool name (LLM-visible function name)")
    title: Optional[str] = Field(None, description="Human-readable title (falls back to name)")
    description: str = Field(..., min_length=1, max_length=500, description="Tool description shown to LLM")
    type: Optional[str] = Field("api_call", description="Tool type: api_call | function")

    # --- api_call fields (mirrors real project apiCalls collection) ---
    script_id: Optional[str] = Field(
        None,
        description="sokt.io function slug. URL = https://flow.sokt.io/func/<script_id>",
    )
    fields: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "JSON-schema style properties dict. "
            "Example: {\"query\": {\"type\": \"string\", \"description\": \"Search query\"}}"
        ),
    )
    required_params: Optional[list] = Field(
        default_factory=list,
        description="List of required parameter names from fields",
    )
    headers: Optional[dict] = Field(default_factory=dict, description="Extra HTTP headers for the API call")
    static_values: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "Fixed values injected at runtime for 'user' source fields. "
            "Keys match field names. These are never exposed to the LLM."
        ),
    )

    # --- function type fields ---
    function_name: Optional[str] = Field(
        None,
        description="Built-in function name (read_file, write_file, run_shell, list_files)",
    )

    org_id: Optional[str] = Field("default", description="Organization ID")
    status: Optional[str] = Field("active", description="Tool status: active | inactive")


class UpdateToolRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    title: Optional[str] = None
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    type: Optional[str] = None
    script_id: Optional[str] = None
    fields: Optional[dict] = None
    required_params: Optional[list] = None
    headers: Optional[dict] = None
    static_values: Optional[dict] = None
    function_name: Optional[str] = None
    status: Optional[str] = None
