import json
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from graph.state import AgentState

PLANNER_SYSTEM_PROMPT = """You are an intelligent task planner. Analyze the user's input and decide how to handle it.

You MUST respond with valid JSON in one of two modes:

MODE 1 — QUESTION (when the goal is ambiguous or you need preferences to do a better job):
{"mode": "question", "question": {"text": "Your clarifying question", "options": ["Option A", "Option B", "Option C", "Option D"]}, "tasks": []}

MODE 2 — TASKS (when you have enough info to execute):
{"mode": "tasks", "question": null, "tasks": [{"title": "Step title", "description": "What to do"}]}

RULES:
- Use MODE 1 (question) when the goal is vague or could go multiple directions. Provide 2-4 clear options.
- Use MODE 2 (tasks) when the goal is clear enough to act on. Number of tasks depends on complexity.
- If the user already answered a question (context contains their answer), proceed to MODE 2 with tasks.
"""


async def _run_planner(state: AgentState, model: str = "gpt-4o-mini", temperature: float = 0.3, system_prompt_override: str = None) -> dict:
    """Core planner logic, parameterized for reuse by both default and dynamic nodes."""
    goal = state["goal"]
    human_input = state.get("human_input")

    user_message = goal
    if human_input:
        user_message = f"{goal}\n\nUser's answer to previous question: {human_input}"

    prompt = system_prompt_override or PLANNER_SYSTEM_PROMPT

    llm = ChatOpenAI(
        model=model,
        api_key=state["api_key"],
        temperature=temperature,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    response = await llm.ainvoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=user_message),
        ]
    )

    parsed = json.loads(response.content)
    mode = parsed.get("mode", "tasks")

    if mode == "question" and parsed.get("question"):
        return {
            "needs_question": True,
            "question_text": parsed["question"]["text"],
            "question_options": parsed["question"].get("options", []),
            "tasks": [],
        }

    tasks = []
    for task in parsed.get("tasks", []):
        tasks.append(
            {
                "id": str(uuid.uuid4())[:8],
                "title": task["title"],
                "description": task["description"],
                "status": "pending",
                "result": None,
            }
        )

    return {
        "needs_question": False,
        "question_text": None,
        "question_options": None,
        "tasks": tasks,
        "human_input": None,
    }


async def planner_node(state: AgentState) -> dict:
    """Default planner node — uses gpt-4o-mini with built-in system prompt."""
    return await _run_planner(state)


def make_planner_node(agent_config: dict):
    """Factory: creates a planner node parameterized by agent DB config."""
    model = agent_config.get("model", "gpt-4o-mini")
    temperature = agent_config.get("temperature", 0.3)
    agent_system_prompt = agent_config.get("system_prompt", "")

    # Build an enhanced planner prompt that includes the agent's system prompt
    custom_prompt = PLANNER_SYSTEM_PROMPT
    if agent_system_prompt:
        custom_prompt = (
            f"You are acting as the planner for an AI agent with the following persona:\n"
            f"---\n{agent_system_prompt}\n---\n\n"
            f"{PLANNER_SYSTEM_PROMPT}"
        )

    async def dynamic_planner_node(state: AgentState) -> dict:
        return await _run_planner(state, model=model, temperature=temperature, system_prompt_override=custom_prompt)

    return dynamic_planner_node
