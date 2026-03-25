from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from graph.state import AgentState

FINAL_ANSWER_PROMPT = """You were given this goal by the user:
"{goal}"

You completed it step by step. Here are the results of each step:

{step_results}

Now produce the FINAL consolidated output for the user. This should be the actual deliverable — not a summary of steps, but the real answer/output they asked for. Combine all step results into one clean, polished response."""


async def _run_synthesizer(state: AgentState, model: str = None, temperature: float = None, system_prompt_override: str = None) -> dict:
    """Core synthesizer logic, parameterized for reuse by both default and dynamic nodes.
    
    Reads configuration from state['user_config'] with fallback to function params and defaults.
    """
    config = state.get("user_config") or {}

    resolved_model = model or config.get("synthesizer_model", "gpt-4o-mini")
    resolved_temp = temperature if temperature is not None else config.get("planner_temperature", 0.4)

    completed = state.get("completed_tasks", [])

    if not completed:
        return {"final_answer": "No steps were completed successfully."}

    step_results = "\n\n".join(
        [f"### {c['title']}\n{c['result']}" for c in completed]
    )

    # Build prompt: override > user_config persona > default
    if system_prompt_override:
        prompt = system_prompt_override
    else:
        agent_persona = config.get("system_prompt", "")
        if agent_persona:
            prompt = (
                f"{agent_persona}\n\n"
                f"The user's goal was: \"{state['goal']}\"\n\n"
                f"Step results:\n{step_results}\n\n"
                f"Now produce the FINAL consolidated output. This should be the actual deliverable — "
                f"not a summary of steps, but the real answer/output they asked for."
            )
        else:
            prompt = FINAL_ANSWER_PROMPT.format(
                goal=state["goal"], step_results=step_results
            )

    llm = ChatOpenAI(
        model=resolved_model,
        api_key=state["api_key"],
        temperature=resolved_temp,
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
    """Default synthesizer node — reads config from state['user_config']."""
    return await _run_synthesizer(state)


def make_synthesizer_node(agent_config: dict):
    """Factory: creates a synthesizer node parameterized by agent DB config.
    
    Injects agent_config values into state['user_config'] before calling _run_synthesizer.
    """
    agent_defaults = {
        "synthesizer_model": agent_config.get("model", "gpt-4o-mini"),
        "planner_temperature": agent_config.get("temperature", 0.4),
        "system_prompt": agent_config.get("system_prompt", ""),
    }

    async def dynamic_synthesizer_node(state: AgentState) -> dict:
        merged_config = {**agent_defaults, **(state.get("user_config") or {})}
        merged_state = {**state, "user_config": merged_config}
        return await _run_synthesizer(merged_state)

    return dynamic_synthesizer_node
