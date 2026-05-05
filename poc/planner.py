import json
from poc.gtwy_client import call_gtwy
from poc.config import PLANNER_BRIDGE_ID
import poc.state_manager as sm


_QUESTIONS_PROMPT = """You are a planning assistant. A user has given you a goal.
Your job is to ask 2-3 focused clarifying questions that will help you build a concrete step-by-step plan.
Keep questions short and specific.

Respond with ONLY a JSON array of question strings. Example:
["What is the target platform?", "Do you have an existing codebase?"]

User goal: {goal}"""

_PLAN_PROMPT = """You are a planning assistant. Based on the goal and clarifying answers below,
create a concrete step-by-step execution plan.

Respond with ONLY a JSON array of task objects. Each task must have:
- "id": sequential number starting at 1
- "title": short task title
- "description": what to do in this step
- "status": "pending"

Goal: {goal}

Q&A:
{qa_section}"""


def run_planning(session: dict) -> list[dict]:
    goal = session["goal"]

    # Step 1: Get clarifying questions from GTWY
    print("\n[planner] Thinking about your goal...\n")
    questions_raw = call_gtwy(
        _QUESTIONS_PROMPT.format(goal=goal),
        bridge_id=PLANNER_BRIDGE_ID,
        thread_id=session["id"],
    )

    try:
        questions = json.loads(questions_raw)
        if not isinstance(questions, list):
            raise ValueError("not a list")
    except Exception:
        # Fallback: treat raw text as a single question
        questions = [questions_raw.strip()]

    # Step 2: Ask user each question in terminal
    print("[planner] A few quick questions before I build your plan:\n")
    for q in questions:
        print(f"  Q: {q}")
        answer = input("  A: ").strip()
        sm.add_qa(session, q, answer)

    # Step 3: Generate plan from goal + answers
    qa_section = "\n".join(
        f"Q: {pair['question']}\nA: {pair['answer']}"
        for pair in session["qa_pairs"]
    )
    print("\n[planner] Building your plan...\n")
    plan_raw = call_gtwy(
        _PLAN_PROMPT.format(goal=goal, qa_section=qa_section),
        bridge_id=PLANNER_BRIDGE_ID,
        thread_id=session["id"],
    )

    try:
        tasks = json.loads(plan_raw)
        if not isinstance(tasks, list):
            raise ValueError("not a list")
    except Exception:
        print(f"[planner] Warning: could not parse plan as JSON. Raw:\n{plan_raw}")
        tasks = [{"id": 1, "title": "Execute goal", "description": plan_raw, "status": "pending"}]

    sm.set_plan(session, tasks)
    return tasks
