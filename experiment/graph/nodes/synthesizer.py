from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from graph.state import AgentState

FINAL_ANSWER_PROMPT = """You were given this goal by the user:
"{goal}"

You completed it step by step. Here are the results of each step:

{step_results}

Now produce the FINAL consolidated output for the user. This should be the actual deliverable — not a summary of steps, but the real answer/output they asked for. Combine all step results into one clean, polished response."""


async def _run_synthesizer(state: AgentState, model: str = "gpt-4o-mini", temperature: float = 0.4, system_prompt_override: str = None) -> dict:
    """Core synthesizer logic, parameterized for reuse by both default and dynamic nodes."""
    completed = state.get("completed_tasks", [])

    if not completed:
        return {"final_answer": "No steps were completed successfully."}

    step_results = "\n\n".join(
        [f"### {c['title']}\n{c['result']}" for c in completed]
    )

    prompt = system_prompt_override or FINAL_ANSWER_PROMPT.format(
        goal=state["goal"], step_results=step_results
    )

    llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
        streaming=True,
    )

    full_text = ""
    async for chunk in llm.astream(
        [
            SystemMessage(content=prompt),
            HumanMessage(content="Produce the final consolidated output now."),
        ]
    ):
        full_text += chunk.content

    return {"final_answer": full_text}


async def synthesizer_node(state: AgentState) -> dict:
    """Default synthesizer node — uses gpt-4o-mini."""
    return await _run_synthesizer(state)


def make_synthesizer_node(agent_config: dict):
    """Factory: creates a synthesizer node parameterized by agent DB config."""
    model = agent_config.get("model", "gpt-4o-mini")
    temperature = agent_config.get("temperature", 0.4)
    agent_system_prompt = agent_config.get("system_prompt", "")

    async def dynamic_synthesizer_node(state: AgentState) -> dict:
        completed = state.get("completed_tasks", [])
        step_results = "\n\n".join(
            [f"### {c['title']}\n{c['result']}" for c in completed]
        ) if completed else ""

        custom_prompt = None
        if agent_system_prompt:
            custom_prompt = (
                f"{agent_system_prompt}\n\n"
                f"The user's goal was: \"{state['goal']}\"\n\n"
                f"Step results:\n{step_results}\n\n"
                f"Now produce the FINAL consolidated output. This should be the actual deliverable — "
                f"not a summary of steps, but the real answer/output they asked for."
            )

        return await _run_synthesizer(state, model=model, temperature=temperature, system_prompt_override=custom_prompt)

    return dynamic_synthesizer_node
