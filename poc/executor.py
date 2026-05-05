from poc.gtwy_client import call_gtwy
from poc.config import EXECUTOR_BRIDGE_ID
import poc.state_manager as sm


_TASK_PROMPT = """You are an AI executor completing one step of a larger plan.

Overall goal: {goal}

Completed steps so far:
{completed}

Your current task:
Title: {title}
Description: {description}

Execute this task thoroughly. Provide your result as clear, concise text."""


def run_execution(session: dict) -> None:
    goal = session["goal"]
    tasks = session["plan"]

    print(f"\n[executor] Running {len(tasks)} task(s)...\n")

    for i, task in enumerate(tasks):
        if task.get("status") == "completed":
            print(f"  [{i+1}/{len(tasks)}] '{task['title']}' — already done, skipping.")
            continue

        print(f"  [{i+1}/{len(tasks)}] {task['title']}")
        print(f"          {task['description']}")

        completed_summary = "\n".join(
            f"- {t['title']}: {t.get('result', '')[:200]}"
            for t in tasks[:i]
            if t.get("status") == "completed"
        ) or "None yet."

        prompt = _TASK_PROMPT.format(
            goal=goal,
            completed=completed_summary,
            title=task["title"],
            description=task["description"],
        )

        sm.update_task(session, i, "running")
        try:
            result = call_gtwy(prompt, bridge_id=EXECUTOR_BRIDGE_ID, thread_id=session["id"])
            sm.update_task(session, i, "completed", result)
            print(f"\n  Result:\n{_indent(result)}\n")
        except Exception as e:
            error_msg = str(e)
            sm.update_task(session, i, "failed", error_msg)
            print(f"  [!] Task failed: {error_msg}\n")

    sm.mark_done(session)
    print("[executor] All tasks complete. State saved to:", sm._session_path(session["id"]))


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
