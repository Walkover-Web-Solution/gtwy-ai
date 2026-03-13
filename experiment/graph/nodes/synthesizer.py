from langchain_openai import ChatOpenAI

from graph.state import AgentState

FINAL_ANSWER_PROMPT = """You were given this goal by the user:
"{goal}"

You completed it step by step. Here are the results of each step:

{step_results}

Now produce the FINAL consolidated output for the user. This should be the actual deliverable — not a summary of steps, but the real answer/output they asked for. Combine all step results into one clean, polished response."""


async def synthesizer_node(state: AgentState) -> dict:
    """Combines all task results into a final answer."""
    completed = state.get("completed_tasks", [])

    if not completed:
        return {"final_answer": "No steps were completed successfully."}

    step_results = "\n\n".join(
        [f"### {c['title']}\n{c['result']}" for c in completed]
    )

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=state["api_key"],
        temperature=0.4,
        streaming=True,
    )

    full_text = ""
    async for chunk in llm.astream(
        [
            {
                "role": "system",
                "content": FINAL_ANSWER_PROMPT.format(
                    goal=state["goal"], step_results=step_results
                ),
            },
            {"role": "user", "content": "Produce the final consolidated output now."},
        ]
    ):
        full_text += chunk.content

    return {"final_answer": full_text}
