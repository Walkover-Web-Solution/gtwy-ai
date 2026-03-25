import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from graph.nodes.tools import TOOLS, TOOLS_BY_NAME
from graph.state import AgentState


async def _run_direct(
    state: AgentState,
    model: str = None,
    temperature: float = None,
    tools: list = None,
    tools_by_name: dict = None,
    system_prompt: str = None,
) -> dict:
    """Direct mode: conversational agent with tools and persistent message history.

    Reads configuration from state['user_config'] with fallback to function params and defaults.

    Designed for agents like FBAI that:
    - Build output incrementally across multiple turns (e.g. one automation step per turn)
    - Return structured JSON like {"response": [..., {"laststep": true/false}]}
    - Need user to click "Next" to trigger the next turn

    State fields used:
      direct_messages  — full message history (persisted across turns)
      built_steps      — all response objects accumulated so far
      human_input      — "continue" or user follow-up message for next turn
    """
    config = state.get("user_config") or {}

    resolved_model = model or config.get("direct_model", "gpt-4o-mini")
    resolved_temp = temperature if temperature is not None else config.get("planner_temperature", 0.3)
    resolved_prompt = system_prompt or config.get("system_prompt") or None

    resolved_tools = tools or TOOLS
    resolved_tools_by_name = tools_by_name or TOOLS_BY_NAME

    llm = ChatOpenAI(
        model=resolved_model,
        api_key=state["api_key"],
        temperature=resolved_temp,
        streaming=True,
    )
    if resolved_tools:
        llm = llm.bind_tools(resolved_tools)

    # Rebuild LangChain message history from stored plain dicts
    history = state.get("direct_messages") or []
    built_steps = list(state.get("built_steps") or [])

    lc_messages = []
    if resolved_prompt:
        lc_messages.append(SystemMessage(content=resolved_prompt))

    for m in history:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))

    # Add the current user turn
    human_input = state.get("human_input") or ""
    if not history:
        # First turn — user's original goal
        user_turn = state["goal"]
    else:
        # Subsequent turn — triggered by "Next" button
        user_turn = human_input if human_input else "continue"

    lc_messages.append(HumanMessage(content=user_turn))

    max_iterations = 10
    final_text = ""

    for _ in range(max_iterations):
        response = await llm.ainvoke(lc_messages)

        if response.tool_calls:
            lc_messages.append(response)
            for tool_call in response.tool_calls:
                tool_fn = resolved_tools_by_name.get(tool_call["name"])
                if tool_fn:
                    try:
                        tool_result = await tool_fn.ainvoke(tool_call["args"])
                    except Exception as e:
                        tool_result = f"Tool error: {e}"
                else:
                    tool_result = f"Unknown tool: {tool_call['name']}"
                lc_messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call["id"],
                ))
        else:
            final_text = response.content
            break

    # Parse JSON response and extract built steps
    last_step = True
    new_response_objects = []
    try:
        parsed = json.loads(final_text)
        new_response_objects = parsed.get("response", [])
        # Check if any object has laststep: false
        for obj in new_response_objects:
            if obj.get("laststep") is False:
                last_step = False
                break
    except Exception:
        # Not JSON — treat as plain text response
        new_response_objects = [{"type": "text", "text": final_text}]

    # Accumulate built steps
    built_steps.extend(new_response_objects)

    # Update message history
    new_history = list(history)
    new_history.append({"role": "user", "content": user_turn})
    new_history.append({"role": "assistant", "content": final_text})

    return {
        "final_answer": final_text,
        "direct_messages": new_history,
        "built_steps": built_steps,
        # Signal to WebSocket whether user needs to click Next
        "needs_question": not last_step,
        "question_text": "__next_step__" if not last_step else None,
    }


async def direct_node(state: AgentState) -> dict:
    """Default direct node — reads config from state['user_config']."""
    return await _run_direct(state)


def make_direct_node(agent_config: dict, tools: list):
    """Factory: creates a direct node parameterized by agent DB config.
    
    Injects agent_config values into state['user_config'] before calling _run_direct.
    """
    tools_by_name = {t.name: t for t in tools}

    agent_defaults = {
        "direct_model": agent_config.get("model", "gpt-4o-mini"),
        "planner_temperature": agent_config.get("temperature", 0.3),
        "system_prompt": agent_config.get("system_prompt", ""),
    }

    async def dynamic_direct_node(state: AgentState) -> dict:
        merged_config = {**agent_defaults, **(state.get("user_config") or {})}
        merged_state = {**state, "user_config": merged_config}
        return await _run_direct(
            merged_state,
            tools=tools,
            tools_by_name=tools_by_name,
        )

    return dynamic_direct_node
