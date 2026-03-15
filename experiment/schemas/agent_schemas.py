from typing import Optional

from pydantic import BaseModel, Field


class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Agent name")
    description: Optional[str] = Field("", max_length=500, description="Agent description")
    system_prompt: Optional[str] = Field(
        "You are a helpful AI assistant.",
        description="System prompt for the agent",
    )
    model: Optional[str] = Field("gpt-4o-mini", description="LLM model to use")
    temperature: Optional[float] = Field(0.3, ge=0, le=2, description="Model temperature")
    max_tokens: Optional[int] = Field(4096, ge=1, le=128000, description="Max output tokens")
    org_id: Optional[str] = Field("default", description="Organization ID")
    tools: Optional[list[str]] = Field(default_factory=list, description="List of tool_ids to attach")
    sub_agents: Optional[list[str]] = Field(default_factory=list, description="List of sub-agent agent_ids for A2A")
    status: Optional[str] = Field("active", description="Agent status: active, inactive, draft")


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=1, le=128000)
    tools: Optional[list[str]] = None
    sub_agents: Optional[list[str]] = None
    status: Optional[str] = None


class InvokeAgentRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="The goal/task for the agent")
    api_key: Optional[str] = Field(None, description="OpenAI API key override")
    org_id: Optional[str] = Field("default", description="Organization ID")
