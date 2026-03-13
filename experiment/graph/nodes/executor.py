from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI

from graph.nodes.tools import TOOLS, TOOLS_BY_NAME
from graph.state import AgentState

EXECUTOR_SYSTEM_PROMPT = """You are a task executor working on a subtask as part of a larger goal.

Overall goal: {goal}

You have access to tools: read_file, write_file, run_shell, list_files, and send_webhook.

RULES:
- Use tools when needed to complete the task (read files, write files, run commands).
- On the FINAL task (is_final_task=True), after producing the complete answer you MUST call send_webhook with the full consolidated final answer.
- Only call send_webhook once, only on the final task, only after everything is done.
- Do NOT call send_webhook on intermediate tasks.
- Be specific and produce real output, not just descriptions."""


async def executor_node(state: AgentState) -> dict:
    """Executes the current task using a ReAct loop with tool calling."""
    tasks = state["tasks"]
    idx = state["current_task_index"]

    if idx >= len(tasks):
        return {}

    task = tasks[idx]
    completed = state.get("completed_tasks", [])
    is_final_task = (idx == len(tasks) - 1)

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=state["api_key"],
        temperature=0.5,
        streaming=True,
    ).bind_tools(TOOLS)

    messages = [
        {
            "role": "system",
            "content": EXECUTOR_SYSTEM_PROMPT.format(goal=state["goal"]),
        },
    ]

    if completed:
        context_summary = "\n".join(
            [f"Step '{c['title']}':\n{c['result']}" for c in completed]
        )
        messages.append(
            {"role": "assistant", "content": f"Previously completed steps:\n{context_summary}"}
        )

    messages.append({
        "role": "user",
        "content": (
            f"Execute this subtask:\n\nTitle: {task['title']}\nDescription: {task['description']}\n\n"
            + ("is_final_task=True — after completing this task, call send_webhook with the full final answer." if is_final_task else "is_final_task=False — do NOT call send_webhook.")
        ),
    })

    result_text = ""

    # ReAct loop: LLM → tool call → result → LLM → ... → final text answer
    while True:
        response = await llm.ainvoke(messages)

        if response.tool_calls:
            messages.append(response)

            for tool_call in response.tool_calls:
                tool_fn = TOOLS_BY_NAME.get(tool_call["name"])
                if tool_fn:
                    tool_result = await tool_fn.ainvoke(tool_call["args"])
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
