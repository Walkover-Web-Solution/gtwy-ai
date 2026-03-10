import json
from typing import AsyncGenerator, List, Dict

from openai import AsyncOpenAI

from planner import plan_tasks

EXECUTOR_SYSTEM_PROMPT = """You are a task executor working on a subtask as part of a larger goal.

Overall goal: {goal}

Execute the given subtask thoroughly. Be specific, actionable, and produce real output (code, text, plans — whatever the task needs). Not just descriptions of what to do, but actually DO it."""

FINAL_ANSWER_PROMPT = """You were given this goal by the user:
"{goal}"

You completed it step by step. Here are the results of each step:

{step_results}

Now produce the FINAL consolidated output for the user. This should be the actual deliverable — not a summary of steps, but the real answer/output they asked for. Combine all step results into one clean, polished response."""


def _sse_event(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data)}


async def run_experiment(goal: str, api_key: str) -> AsyncGenerator[dict, None]:
    # Phase 1: Planning
    yield _sse_event("planning", {"message": "Analyzing your request..."})

    try:
        plan_result = await plan_tasks(goal, api_key)
    except Exception as e:
        yield _sse_event("error", {"message": f"Planning failed: {str(e)}"})
        return

    mode = plan_result.get("mode", "direct")
    direct_response = plan_result.get("direct_response", "")
    question = plan_result.get("question", None)
    tasks = plan_result.get("tasks", [])

    # Mode: question — AI needs clarification, show options
    if mode == "question" and question:
        yield _sse_event("question", {
            "text": question.get("text", ""),
            "options": question.get("options", []),
        })
        return

    # Mode: direct — greetings, simple questions
    if mode == "direct" or (not tasks and direct_response):
        yield _sse_event("direct_response", {"message": direct_response})
        yield _sse_event("done", {"summary": direct_response})
        return

    yield _sse_event(
        "plan_ready",
        {"tasks": [{"id": t["id"], "title": t["title"], "description": t["description"], "status": "pending"} for t in tasks]},
    )

    # Phase 2: Execute each task sequentially
    client = AsyncOpenAI(api_key=api_key)
    completed_context: List[Dict] = []

    for task in tasks:
        task["status"] = "in_progress"
        yield _sse_event("task_start", {"task_id": task["id"], "title": task["title"]})

        try:
            context_messages = []
            if completed_context:
                context_summary = "\n".join(
                    [f"Step '{c['title']}':\n{c['result']}" for c in completed_context]
                )
                context_messages.append(
                    {"role": "assistant", "content": f"Previously completed steps:\n{context_summary}"}
                )

            system_prompt = EXECUTOR_SYSTEM_PROMPT.format(goal=goal)

            stream = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    *context_messages,
                    {
                        "role": "user",
                        "content": f"Execute this subtask:\n\nTitle: {task['title']}\nDescription: {task['description']}",
                    },
                ],
                temperature=0.5,
                stream=True,
            )

            full_result = ""
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_result += delta.content
                    yield _sse_event(
                        "task_progress",
                        {"task_id": task["id"], "chunk": delta.content},
                    )

            task["status"] = "completed"
            task["result"] = full_result
            completed_context.append({"title": task["title"], "result": full_result})

            yield _sse_event(
                "task_done",
                {"task_id": task["id"], "result": full_result},
            )

        except Exception as e:
            task["status"] = "failed"
            yield _sse_event(
                "task_failed",
                {"task_id": task["id"], "error": str(e)},
            )

    # Phase 3: Final answer — synthesize all step results into one deliverable
    if completed_context:
        yield _sse_event("final_answer_start", {"message": "Preparing final output..."})

        step_results = "\n\n".join(
            [f"### {c['title']}\n{c['result']}" for c in completed_context]
        )

        try:
            stream = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": FINAL_ANSWER_PROMPT.format(goal=goal, step_results=step_results)},
                    {"role": "user", "content": "Produce the final consolidated output now."},
                ],
                temperature=0.4,
                stream=True,
            )

            final_text = ""
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    final_text += delta.content
                    yield _sse_event("final_answer_progress", {"chunk": delta.content})

            yield _sse_event("done", {"summary": final_text})
        except Exception as e:
            yield _sse_event("done", {"summary": f"Steps completed but final synthesis failed: {str(e)}"})
    else:
        yield _sse_event("done", {"summary": "No steps were completed successfully."})
