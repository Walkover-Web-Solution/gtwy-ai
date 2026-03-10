import json
import uuid
from typing import List, Dict

from openai import AsyncOpenAI

PLANNER_SYSTEM_PROMPT = """You are an intelligent task planner. Analyze the user's input and decide how to handle it.

You MUST respond with one of three modes:

MODE 1 — DIRECT RESPONSE (greetings, simple questions):
{"mode": "direct", "direct_response": "Your reply here", "question": null, "tasks": []}

MODE 2 — QUESTION (when the goal is ambiguous or you need preferences to do a better job):
{"mode": "question", "direct_response": "", "question": {"text": "Your clarifying question", "options": ["Option A", "Option B", "Option C", "Option D"]}, "tasks": []}

MODE 3 — TASKS (when you have enough info to execute):
{"mode": "tasks", "direct_response": "", "question": null, "tasks": [{"title": "Step title", "description": "What to do"}]}

RULES:
- Use MODE 2 (question) when the goal is vague or could go multiple directions. Provide 2-4 clear options. Keep options short and specific.
- Use MODE 3 (tasks) when the goal is clear enough to act on. Number of tasks depends on complexity — could be 1 or 10+.
- Use MODE 1 (direct) only for greetings, casual chat, or questions with obvious answers.
- If the user already answered a question (message contains "Selected: ..."), proceed to MODE 3 with tasks.

Examples:
- "hi" → mode: "direct"
- "build an app" → mode: "question" (what kind of app? what tech stack?)
- "build a REST API for todo app using FastAPI" → mode: "tasks"
- "write a poem" → mode: "question" (what topic? what style?)
- "write a haiku about rain" → mode: "tasks" (clear enough)"""


async def plan_tasks(goal: str, api_key: str) -> Dict:
    client = AsyncOpenAI(api_key=api_key)

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    content = response.choices[0].message.content
    parsed = json.loads(content)

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
        "mode": parsed.get("mode", "direct"),
        "direct_response": parsed.get("direct_response", ""),
        "question": parsed.get("question", None),
        "tasks": tasks,
    }
