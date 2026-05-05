import json
import os
import time
from poc.config import STATE_DIR


def _session_path(session_id: str) -> str:
    return os.path.join(STATE_DIR, f"{session_id}.json")


def create_session(goal: str) -> dict:
    session_id = f"session_{int(time.time())}"
    session = {
        "id": session_id,
        "goal": goal,
        "qa_pairs": [],
        "plan": [],
        "status": "planning",
    }
    save_session(session)
    return session


def save_session(session: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _session_path(session["id"])
    with open(path, "w") as f:
        json.dump(session, f, indent=2)


def load_session(session_id: str) -> dict:
    path = _session_path(session_id)
    with open(path) as f:
        return json.load(f)


def list_sessions() -> list[str]:
    if not os.path.isdir(STATE_DIR):
        return []
    return sorted(
        f.replace(".json", "")
        for f in os.listdir(STATE_DIR)
        if f.endswith(".json")
    )


def add_qa(session: dict, question: str, answer: str) -> None:
    session["qa_pairs"].append({"question": question, "answer": answer})
    save_session(session)


def set_plan(session: dict, tasks: list[dict]) -> None:
    session["plan"] = tasks
    session["status"] = "executing"
    save_session(session)


def update_task(session: dict, task_index: int, status: str, result: str = None) -> None:
    session["plan"][task_index]["status"] = status
    if result is not None:
        session["plan"][task_index]["result"] = result
    save_session(session)


def mark_done(session: dict) -> None:
    session["status"] = "done"
    save_session(session)
