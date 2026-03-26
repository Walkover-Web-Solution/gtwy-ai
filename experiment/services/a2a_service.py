import os

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from db.a2a_db_service import check_caller_permission, discover_agents, get_agent_card, register_agent_card
from db.agent_db_service import get_agent
from services.tool_registry import load_tools_for_agent

# In-memory conversation history store keyed by thread_id
# Allows sub-agents to maintain context across multiple calls for the same task
_thread_conversations: dict[str, list] = {}


async def build_agent_card(agent_id: str) -> dict:
    """Build and register an A2A agent card from the agent's DB config."""
    agent = await get_agent(agent_id)
    if not agent:
        return None

    card = {
        "name": agent["name"],
        "description": agent.get("description", ""),
        "capabilities": [],
        "input_modes": ["text"],
        "output_modes": ["text"],
        "model": agent.get("model", "gpt-4o-mini"),
        "endpoint": f"/agents/{agent_id}/a2a/invoke",
        "discoverable": True,
        "allowed_callers": [],
    }

    registered = await register_agent_card(agent_id, card, agent.get("org_id", "default"))
    return registered


async def get_or_build_agent_card(agent_id: str) -> dict | None:
    """Get an existing agent card, or build one from the agent config."""
    existing = await get_agent_card(agent_id)
    if existing:
        return existing
    return await build_agent_card(agent_id)


async def discover_available_agents(org_id: str = None) -> list[dict]:
    """Discover all agents available for A2A communication."""
    return await discover_agents(org_id)


async def invoke_agent_a2a(target_agent_id: str, input_text: str, caller_agent_id: str = None, api_key: str = None, thread_id: str = None) -> str:
    """Invoke an agent via A2A protocol. Returns the agent's final answer as a string.
    
    If thread_id is provided, conversation history is preserved across calls so the
    sub-agent retains context from previous interactions on the same task.
    """

    # Check permission if caller is specified
    if caller_agent_id:
        allowed = await check_caller_permission(target_agent_id, caller_agent_id)
        if not allowed:
            return f"A2A Error: Agent '{caller_agent_id}' is not allowed to invoke '{target_agent_id}'."

    # Get target agent config
    target_agent = await get_agent(target_agent_id)
    if not target_agent:
        return f"A2A Error: Agent '{target_agent_id}' not found."

    if target_agent.get("status") != "active":
        return f"A2A Error: Agent '{target_agent_id}' is not active."

    # Resolve API key
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_api_key:
        return "A2A Error: No API key available."

    # Load tools for target agent (but exclude A2A tools to prevent infinite recursion)
    tools = await load_tools_for_agent(target_agent_id)

    # Simple direct invocation: use the agent's system prompt + tools to answer
    llm = ChatOpenAI(
        model=target_agent.get("model", "gpt-4o-mini"),
        api_key=resolved_api_key,
        temperature=target_agent.get("temperature", 0.3),
    )

    if tools:
        llm = llm.bind_tools(tools)

    # Restore or initialise conversation history for this thread
    conv_key = f"{target_agent_id}:{thread_id}" if thread_id else None
    if conv_key and conv_key in _thread_conversations:
        messages = _thread_conversations[conv_key]
        messages.append(HumanMessage(content=input_text))
    else:
        messages = [
            SystemMessage(content=target_agent.get("system_prompt", "You are a helpful AI assistant.")),
            HumanMessage(content=input_text),
        ]

    # ReAct loop: handle tool calls
    tools_by_name = {t.name: t for t in tools} if tools else {}
    max_iterations = 10
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        response = await llm.ainvoke(messages)

        if response.tool_calls:
            messages.append(response)

            for tool_call in response.tool_calls:
                tool_fn = tools_by_name.get(tool_call["name"])
                if tool_fn:
                    try:
                        tool_result = await tool_fn.ainvoke(tool_call["args"])
                    except Exception as e:
                        tool_result = f"Tool error: {e}"
                else:
                    tool_result = f"Unknown tool: {tool_call['name']}"

                messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call["id"],
                ))
        else:
            messages.append(response)
            # Persist conversation history if thread_id was provided
            if conv_key:
                _thread_conversations[conv_key] = messages
            return response.content

    return "A2A Error: Max iterations reached without a final answer."


def create_a2a_tool(sub_agent_id: str, sub_agent_config: dict, thread_id: str = None) -> StructuredTool:
    """Create a LangChain tool that wraps A2A invocation of a sub-agent.

    When the parent agent's LLM decides to delegate, it calls this tool,
    which internally invokes the sub-agent and returns its response.
    
    If thread_id is provided, the sub-agent retains conversation context across
    multiple calls for the same task (same thread_id = same context).
    """
    agent_name = sub_agent_config.get("name", sub_agent_id)
    agent_desc = sub_agent_config.get("description", f"Sub-agent: {agent_name}")

    async def invoke_sub_agent(task: str) -> str:
        """Delegate a task to the sub-agent and return its response."""
        result = await invoke_agent_a2a(sub_agent_id, task, thread_id=thread_id)
        return result

    return StructuredTool.from_function(
        coroutine=invoke_sub_agent,
        name=f"ask_{agent_name.lower().replace(' ', '_')}",
        description=f"Delegate a task to sub-agent '{agent_name}': {agent_desc}. "
                     f"Use this when the task matches this agent's expertise.",
    )
