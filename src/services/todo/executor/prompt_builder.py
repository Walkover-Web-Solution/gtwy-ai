import json

from globals import logger
from src.services.todo.executor.constants import WORKER_RESPONSE_SCHEMA, WORKER_SYSTEM_PROMPT_TEMPLATE


def build_dependency_context(task: dict, all_tasks: dict) -> str:
    """One `<dep_id>: <result>` line per completed dependency. No header,
    no title, no metadata — just the raw result the agent needs to resolve
    references in execution_details."""
    lines = []
    for dep_id in task.get("dependencies", []):
        dep = all_tasks.get(dep_id) or {}
        if dep.get("status") != "completed":
            continue
        lines.append(f"{dep_id}: {_as_text(dep.get('result'), default='')}")
    return "\n".join(lines)


def _as_text(value, default=""):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)


def _render_human_loop_block(task: dict) -> str:
    """Render the prior Q&A + answer block when the user is continuing a
    waiting_for_user task. Empty string when this is a fresh turn."""
    human_response = task.get("human_response")
    prior_questions = task.get("questions")
    prior_history = task.get("history") if isinstance(task.get("history"), dict) else None

    if not human_response and not prior_questions and not prior_history:
        return ""

    parts = ["USER_CLARIFICATION_ANSWER"]
    if prior_history and prior_history.get("goal"):
        parts.append(f"Prior goal: {prior_history['goal']}")
    if prior_questions:
        parts.append("Questions you asked:")
        parts.append(_as_text(prior_questions))
    if human_response:
        parts.append("User answer(s):")
        parts.append(_as_text(human_response))
    if prior_history and prior_history.get("notes"):
        parts.append(f"Prior notes: {prior_history['notes']}")
    return "\n".join(parts) + "\n\n"


def build_worker_system_prompt(
    task: dict,
    filtered_tool_names: list,
    all_tasks: dict | None = None,
    prompt_template: str = WORKER_SYSTEM_PROMPT_TEMPLATE,
) -> str:
    """Compose the system prompt for a worker LLM call."""
    dependency_context = build_dependency_context(task, all_tasks or {})

    format_kwargs = {
        "title": task.get("title", ""),
        "task_description": task.get("task_description", ""),
        "execution_details": _as_text(
            task.get("execution_details"),
            default="(none — use task_description and tool schema)",
        ),
        "human_response_block": _render_human_loop_block(task),
        "tool_list": ", ".join(filtered_tool_names) if filtered_tool_names else "none",
        "schema": WORKER_RESPONSE_SCHEMA,
    }

    try:
        base_prompt = prompt_template.format(**format_kwargs)
    except Exception as err:
        logger.error(f"Worker prompt template format error, using default: {err}")
        base_prompt = WORKER_SYSTEM_PROMPT_TEMPLATE.format(**format_kwargs)

    return f"{base_prompt}\n\n{dependency_context}\n" if dependency_context else base_prompt


def build_worker_user_message(task_id: str, task: dict) -> str:
    return (
        f"Execute task {task_id}: {task.get('title', '')}. "
        "Call the assigned tool/agent now using execution_details and return JSON only."
    )

def build_a2a_initial_user_message(task: dict, all_tasks: dict | None = None) -> str:
    """First-turn user message for an A2A connected-agent task.

    Always carries `execution_details` (the planner's self-sufficient param
    string). When the task has completed dependencies, their results are
    appended so the agent can resolve references like "use orderGroup from
    task_2 output". `title` / `task_description` stay FE-only; the JSON
    envelope contract lives in the agent's own bridge system prompt.
    """
    exec_details = _as_text(task.get("execution_details"), default="")
    dep_context = build_dependency_context(task, all_tasks or {})
    if exec_details and dep_context:
        return f"{exec_details}\n\n{dep_context}"
    return exec_details or dep_context


def _format_human_response_for_user_turn(human_response, prior_questions) -> str:
    """Render the user's answer into a clean user-turn message.

    Handles three input shapes the FE may send:
      - str                                              → passed through.
      - dict like {"q1": "yes", "q2": "alice@x.com"}     → mapped to question text.
      - list like [{"id": "q1", "answer": "yes"}, …]     → mapped to question text.
    Other shapes are JSON-dumped as a safe fallback."""
    if isinstance(human_response, str):
        return human_response

    q_map = {
        q.get("id"): q.get("question")
        for q in (prior_questions or [])
        if isinstance(q, dict) and q.get("id")
    }

    def _line(qid, ans):
        q_text = q_map.get(qid)
        if q_text:
            return f"[{qid}] {q_text} → {ans}"
        return f"[{qid}] {ans}" if qid else str(ans)

    if isinstance(human_response, dict):
        return "\n".join(_line(qid, ans) for qid, ans in human_response.items())

    if isinstance(human_response, list):
        lines = []
        for item in human_response:
            if isinstance(item, dict):
                lines.append(_line(item.get("id"), item.get("answer")))
            else:
                lines.append(str(item))
        return "\n".join(lines)

    if human_response is None:
        return ""
    return json.dumps(human_response, ensure_ascii=False)


def build_a2a_continuation_user_message(task: dict) -> str:
    """Continuation-turn user message: the user's answer to the agent's prior
    questions, formatted so each answer is paired with its question text.
    Returns "" when there is no human_response (caller should fall back to the
    initial-turn builder in that case).

    The trailing reminder line is load-bearing: OpenAI's `text.format` /
    `response_format = {"type": "json_object"}` mode rejects the request
    unless the literal word "json" appears somewhere in the input messages.
    It also nudges the model to re-emit the full envelope (status / result /
    questions / error / history) rather than free-form prose."""
    answer = _format_human_response_for_user_turn(
        task.get("human_response"), task.get("questions") or [],
    )
    if not answer:
        return ""
    return (
        f"{answer}\n\n"
        "Reply with a single JSON object (status, result, questions, error, history) — no prose around it."
    )


def build_a2a_conversation(task: dict, all_tasks: dict | None = None) -> list:
    """Replay the prior user/assistant turn into `configuration.conversation`
    so the connected agent sees history the same way the planner does — as
    past turns, not as a dump pasted into the current user message.

    Returns [] on the first turn (no human_response). On a continuation it
    seeds two messages:
      • user      — the first-turn message (execution_details + dep results
                    if any).
      • assistant — the agent's last JSON response (questions + history,
                    reconstructed from task state)."""
    if not task.get("human_response"):
        return []

    prior_user_message = build_a2a_initial_user_message(task, all_tasks)
    prior_assistant_payload = {
        "status": "waiting_for_user",
        "questions": task.get("questions") or [],
    }
    prior_history = task.get("history") if isinstance(task.get("history"), dict) else None
    if prior_history:
        prior_assistant_payload["history"] = prior_history
    conversation = []
    if prior_user_message:
        conversation.append({"role": "user", "content": prior_user_message})
    conversation.append({
        "role": "assistant",
        "content": json.dumps(prior_assistant_payload, ensure_ascii=False),
    })
    return conversation
