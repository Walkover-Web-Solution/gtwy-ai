from typing import Optional

from pydantic import BaseModel, Field


class AgentCardRequest(BaseModel):
    capabilities: Optional[list[str]] = Field(default_factory=list, description="Agent capabilities")
    input_modes: Optional[list[str]] = Field(default_factory=lambda: ["text"], description="Supported input modes")
    output_modes: Optional[list[str]] = Field(default_factory=lambda: ["text"], description="Supported output modes")
    discoverable: Optional[bool] = Field(True, description="Whether agent is discoverable via A2A")
    allowed_callers: Optional[list[str]] = Field(
        default_factory=list,
        description="Agent IDs allowed to call this agent. Empty = all agents in org.",
    )


class A2AInvokeRequest(BaseModel):
    input_text: str = Field(..., min_length=1, description="Input text/task for the target agent")
    caller_agent_id: Optional[str] = Field(None, description="Agent ID of the caller (for permission check)")
    api_key: Optional[str] = Field(None, description="OpenAI API key override")


class LinkSubAgentRequest(BaseModel):
    sub_agent_id: str = Field(..., description="Agent ID to link as sub-agent")
