from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from graph.nodes.tools import TOOLS, TOOLS_BY_NAME
from graph.state import AgentState

EXECUTOR_SYSTEM_PROMPT = """You are a task executor working on a subtask as part of a larger goal.

Overall goal: {goal}

You have access to tools: {tool_names}.

RULES:
- Use tools when needed to complete the task (read files, write files, run commands, call APIs, delegate to sub-agents).
- Be specific and produce real output, not just descriptions."""


async def _run_executor(state: AgentState, model: str = "gpt-4o-mini", temperature: float = 0.5, tools: list = None, tools_by_name: dict = None, system_prompt_override: str = None) -> dict:
    """Core executor logic, parameterized for reuse by both default and dynamic nodes."""
    resolved_tools = tools or TOOLS
    resolved_tools_by_name = tools_by_name or TOOLS_BY_NAME

    tasks = state["tasks"]
    idx = state["current_task_index"]

    if idx >= len(tasks):
        return {}

    task = tasks[idx]
    completed = state.get("completed_tasks", [])

    tool_names = ", ".join(resolved_tools_by_name.keys())
    prompt = system_prompt_override or EXECUTOR_SYSTEM_PROMPT.format(goal=state["goal"], tool_names=tool_names)

    llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
        streaming=True,
    ).bind_tools(resolved_tools)

    messages = [
        SystemMessage(content=prompt),
    ]

    if completed:
        context_summary = "\n".join(
            [f"Step '{c['title']}':\n{c['result']}" for c in completed]
        )
        messages.append(AIMessage(content=f"Previously completed steps:\n{context_summary}"))

    messages.append(HumanMessage(
        content=f"Execute this subtask:\n\nTitle: {task['title']}\nDescription: {task['description']}",
    ))

    result_text = ""
    max_iterations = 15

    # ReAct loop: LLM → tool call → result → LLM → ... → final text answer
    for _ in range(max_iterations):
        response = await llm.ainvoke(messages)

        if response.tool_calls:
            messages.append(response)

            for tool_call in response.tool_calls:
                tool_fn = resolved_tools_by_name.get(tool_call["name"])
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
            result_text = response.content
            break

    updated_tasks = list(tasks)
    updated_tasks[idx] = {**task, "status": "completed", "result": result_text}

    new_completed = list(completed)
    new_completed.append({"title": task["title"], "result": result_text})

    return {
        "tasks": updated_tasks,
        "completed_tasks": new_completed,
        "current_task_index": idx + 1,
    }


async def executor_node(state: AgentState) -> dict:
    """Default executor node — uses built-in tools and gpt-4o-mini."""
    return await _run_executor(state)


def make_executor_node(agent_config: dict, tools: list):
    """Factory: creates an executor node with dynamic tools and agent config."""
    model = agent_config.get("model", "gpt-4o-mini")
    temperature = agent_config.get("temperature", 0.5)
    agent_system_prompt = agent_config.get("system_prompt", "")

    tools_by_name = {t.name: t for t in tools}
    tool_names = ", ".join(tools_by_name.keys())

    custom_prompt = None
    if agent_system_prompt:
        custom_prompt = (
            f"{agent_system_prompt}\n\n"
            f"You are executing a subtask as part of a larger goal: {{goal}}\n"
            f"Available tools: {tool_names}\n\n"
            f"RULES:\n"
            f"- Use tools when needed to complete the task.\n"
            f"- Be specific and produce real output, not just descriptions."
        )

    async def dynamic_executor_node(state: AgentState) -> dict:
        prompt = custom_prompt.replace("{goal}", state["goal"]) if custom_prompt else None
        return await _run_executor(
            state,
            model=model,
            temperature=temperature,
            tools=tools,
            tools_by_name=tools_by_name,
            system_prompt_override=prompt,
        )

    return dynamic_executor_node
