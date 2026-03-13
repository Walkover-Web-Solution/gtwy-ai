from langchain_openai import ChatOpenAI

from graph.state import AgentState

EXECUTOR_SYSTEM_PROMPT = """You are a task executor working on a subtask as part of a larger goal.

Overall goal: {goal}

Execute the given subtask thoroughly. Be specific, actionable, and produce real output (code, text, plans — whatever the task needs). Not just descriptions of what to do, but actually DO it."""


async def executor_node(state: AgentState) -> dict:
    """Executes the current task. Streams chunks via callback (handled by astream_events)."""
    tasks = state["tasks"]
    idx = state["current_task_index"]

    if idx >= len(tasks):
        return {}

    task = tasks[idx]
    completed = state.get("completed_tasks", [])

    context_messages = []
    if completed:
        context_summary = "\n".join(
            [f"Step '{c['title']}':\n{c['result']}" for c in completed]
        )
        context_messages.append(
            {"role": "assistant", "content": f"Previously completed steps:\n{context_summary}"}
        )

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=state["api_key"],
        temperature=0.5,
    )

    response = await llm.ainvoke(
        [
            {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT.format(goal=state["goal"])},
            *context_messages,
            {
                "role": "user",
                "content": f"Execute this subtask:\n\nTitle: {task['title']}\nDescription: {task['description']}",
            },
        ]
    )

    result_text = response.content

    updated_tasks = list(tasks)
    updated_tasks[idx] = {**task, "status": "completed", "result": result_text}

    new_completed = list(completed)
    new_completed.append({"title": task["title"], "result": result_text})

    return {
        "tasks": updated_tasks,
        "completed_tasks": new_completed,
        "current_task_index": idx + 1,
    }
