import json
import uuid

from langchain_openai import ChatOpenAI

from graph.state import AgentState

PLANNER_SYSTEM_PROMPT = """You are an intelligent task planner. Analyze the user's input and decide how to handle it.

You MUST respond with one of two modes:

MODE 1 — QUESTION (when the goal is ambiguous or you need preferences to do a better job):
{"mode": "question", "question": {"text": "Your clarifying question", "options": ["Option A", "Option B", "Option C", "Option D"]}, "tasks": []}

MODE 2 — TASKS (when you have enough info to execute):
{"mode": "tasks", "question": null, "tasks": [{"title": "Step title", "description": "What to do"}]}

RULES:
- Use MODE 1 (question) when the goal is vague or could go multiple directions. Provide 2-4 clear options.
- Use MODE 2 (tasks) when the goal is clear enough to act on. Number of tasks depends on complexity.
- If the user already answered a question (context contains their answer), proceed to MODE 2 with tasks.
"""


async def planner_node(state: AgentState) -> dict:
    """Creates a task plan from the user's goal using LLM."""
    goal = state["goal"]
    human_input = state.get("human_input")

    user_message = goal
    if human_input:
        user_message = f"{goal}\n\nUser's answer to previous question: {human_input}"

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=state["api_key"],
        temperature=0.3,
    )

    response = await llm.ainvoke(
        [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
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
